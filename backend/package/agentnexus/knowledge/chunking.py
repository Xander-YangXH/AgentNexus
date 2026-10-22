"""中文友好的分块（阶段 2，decisions/0014）。

约束：bge-small-zh 最多读 512 个 token，超出部分会被静默截断（丢失）。所以每块目标 300、硬上限 480（给特殊 token 留余量）。
规则：
1. 先按段落、再按句子边界（。！？；!?; 与换行）切成“句子”，尽量不在句子中间切断；
2. 贪心地把句子装进当前块，装不下就另起一块；
3. 相邻块重叠约 50 个 token（把上一块末尾的几句带到下一块开头），避免一个意思正好被切在交界处、两边都不完整；
   重叠只取整句：上一块最后一句就超过重叠上限时，这次不重叠——半句话会制造语义残缺的片段，反而干扰检索；
4. 单句超过硬上限时才强制按字符切开（例如没有标点的长表格行）；
6. Markdown 按标题切分（heading_boundaries）：遇到标题行就另起一块，重叠也不跨标题。
   一个块里混进几个主题时，向量被“平均”掉：30 篇条目合成一个文件时第一名命中率从 0.90 掉到 0.73（2.4 实测）。
5. 计数有字符下限（字符数 / 6）：bge 的 WordPiece 把超过 100 个字符的“词”整体记成 1 个 [UNK]，
   一段 3MB 的 base64 / 长 URL 只算 1 个 token，不加下限就会整段成为一块（实测踩到）。
每块记录来自哪一页（PDF），回答时可以标注出处。
"""

import math
import re
from dataclasses import dataclass

from agentnexus.knowledge.parsing import Section
from agentnexus.knowledge.tokens import TokenCounter

# 句子结束：中英文句末标点（可跟引号 / 括号）或换行；保留标点在句子末尾
_SENTENCE = re.compile(r"[^。！？；!?;\n]*(?:[。！？；!?;][”’」』）)]*|\n|$)")


_HEADING = re.compile(r"#{1,6}\s")

# 正常文本里一个 token 平均远不到 6 个字符（中文 1 个、英文约 4 个），这个下限只拦异常输入
MAX_CHARS_PER_TOKEN = 6


class _AtLeastChars:
    """计数取 max(模型 token 数, 字符数 / 6)：分块的大小判断用它，存进库的 token_count 仍是模型的真实计数。"""

    def __init__(self, counter: TokenCounter):
        self._counter = counter

    def count(self, text: str) -> int:
        return max(self._counter.count(text), math.ceil(len(text) / MAX_CHARS_PER_TOKEN))


@dataclass(frozen=True)
class Chunk:
    seq: int
    text: str
    token_count: int
    page: int | None


def _raw_sentences(text: str) -> list[str]:
    """切句但保留原文里的空白：句子之间的空格、换行（段落结构）都留在句子里，拼回去与原文一致。

    第一版对每句 strip() 后直接拼接，Markdown 的标题和正文粘成一行，英文 "Really? Yes" 变成 "Really?Yes"
    （2.3 在检索测试页面上看到的）。纯空白的片段（例如单独的换行）并到前一句的末尾。
    """
    out: list[str] = []
    for piece in _SENTENCE.findall(text):
        if not piece:
            continue
        if piece.isspace():
            if out:
                out[-1] += piece
            continue
        out.append(piece)
    return out


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _raw_sentences(text) if s.strip()]


def _hard_split(sentence: str, counter: TokenCounter, limit: int) -> list[str]:
    """单句超长：按字符二分找到不超过上限的最长前缀，逐段切下。

    counter 必须带字符下限（_AtLeastChars）：超过 limit * 6 个字符的文本一定超限，所以只对不超过这个长度的前缀计数。
    否则每切一段都要把剩下的整段（可能几 MB）重新分词一遍，耗时随长度平方增长。
    """
    max_chars = limit * MAX_CHARS_PER_TOKEN
    pieces, rest = [], sentence
    while rest:
        if len(rest) <= max_chars and counter.count(rest) <= limit:
            pieces.append(rest)
            break
        lo, hi = 1, min(len(rest), max_chars)
        while lo < hi:  # 找最大的 k，使 rest[:k] 不超过上限
            mid = (lo + hi + 1) // 2
            if counter.count(rest[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        pieces.append(rest[:lo])
        rest = rest[lo:]
    return pieces


def chunk_sections(
    sections: list[Section],
    counter: TokenCounter,
    *,
    target_tokens: int = 300,
    hard_limit: int = 480,
    overlap_tokens: int = 50,
    heading_boundaries: bool = False,
) -> list[Chunk]:
    assert 0 <= overlap_tokens < target_tokens <= hard_limit
    model_counter, counter = counter, _AtLeastChars(counter)
    # 1) 切成带页码的句子；超长句子先强制切开
    units: list[tuple[str, int, int | None]] = []  # (文本, token 数, 页码)
    for section in sections:
        start = len(units)
        for sentence in _raw_sentences(section.text):
            for piece in _hard_split(sentence, counter, hard_limit):
                units.append((piece, counter.count(piece), section.page))
        if len(units) > start and not units[-1][0][-1].isspace():  # 跨页（跨 section）处断开，不让两页的字粘在一起
            text, n, page = units[-1]
            units[-1] = (text + "\n", n, page)

    chunks: list[Chunk] = []
    current: list[tuple[str, int, int | None]] = []

    def tokens(units) -> int:
        return sum(u[1] for u in units)

    def emit(units):
        text = "".join(u[0] for u in units).strip()
        chunks.append(Chunk(len(chunks), text, model_counter.count(text), units[0][2]))

    def overlap_tail(units):
        """从末尾往前取句子，凑到不超过 overlap_tokens；至少留一句不带（否则下一块会和这块完全重复）。"""
        tail = []
        for unit in reversed(units[1:]):
            if tokens(tail) + unit[1] > overlap_tokens:
                break
            tail.insert(0, unit)
        return tail

    def is_heading(unit) -> bool:
        return heading_boundaries and bool(_HEADING.match(unit[0].lstrip()))

    for unit in units:
        # 新标题开始新的一块（不带重叠）；当前块只有标题时不切，避免“# 文档标题”单独成块
        if current and is_heading(unit) and not all(is_heading(u) for u in current):
            emit(current)
            current = []
        elif current and tokens(current) + unit[1] > target_tokens:
            emit(current)
            current = overlap_tail(current)
            if tokens(current) + unit[1] > hard_limit:  # 加上重叠会超过硬上限：放弃重叠，保证不超限
                current = []
        current.append(unit)
    # 只在“追加新句子之前”才输出一块，所以循环结束时 current 的最后一句一定是新内容，
    # 不会出现“最后一块只是上一块的重叠”（第一版为此多写了一个计数变量，变异检查发现它是多余的）
    if current:
        emit(current)
    return chunks
