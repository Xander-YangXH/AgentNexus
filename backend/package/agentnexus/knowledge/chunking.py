"""中文友好的分块（阶段 2，decisions/0014）。

约束：bge-small-zh 最多读 512 个 token，超出部分会被静默截断（丢失）。所以每块目标 300、硬上限 480（给特殊 token 留余量）。
规则：
1. 先按段落、再按句子边界（。！？；!?; 与换行）切成“句子”，尽量不在句子中间切断；
2. 贪心地把句子装进当前块，装不下就另起一块；
3. 相邻块重叠约 50 个 token（把上一块末尾的几句带到下一块开头），避免一个意思正好被切在交界处、两边都不完整；
   重叠只取整句：上一块最后一句就超过重叠上限时，这次不重叠——半句话会制造语义残缺的片段，反而干扰检索；
4. 单句超过硬上限时才强制按字符切开（例如没有标点的长表格行）。
每块记录来自哪一页（PDF），回答时可以标注出处。
"""

import re
from dataclasses import dataclass

from agentnexus.knowledge.parsing import Section
from agentnexus.knowledge.tokens import TokenCounter

# 句子结束：中英文句末标点（可跟引号 / 括号）或换行；保留标点在句子末尾
_SENTENCE = re.compile(r"[^。！？；!?;\n]*(?:[。！？；!?;][”’」』）)]*|\n|$)")


@dataclass(frozen=True)
class Chunk:
    seq: int
    text: str
    token_count: int
    page: int | None


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip()]


def _hard_split(sentence: str, counter: TokenCounter, limit: int) -> list[str]:
    """单句超长：按字符二分找到不超过上限的最长前缀，逐段切下。"""
    pieces, rest = [], sentence
    while rest:
        if counter.count(rest) <= limit:
            pieces.append(rest)
            break
        lo, hi = 1, len(rest)
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
) -> list[Chunk]:
    assert 0 <= overlap_tokens < target_tokens <= hard_limit
    # 1) 切成带页码的句子；超长句子先强制切开
    units: list[tuple[str, int, int | None]] = []  # (文本, token 数, 页码)
    for section in sections:
        for sentence in split_sentences(section.text):
            for piece in _hard_split(sentence, counter, hard_limit):
                units.append((piece, counter.count(piece), section.page))

    chunks: list[Chunk] = []
    current: list[tuple[str, int, int | None]] = []

    def tokens(units) -> int:
        return sum(u[1] for u in units)

    def emit(units):
        text = "".join(u[0] for u in units)
        chunks.append(Chunk(len(chunks), text, counter.count(text), units[0][2]))

    def overlap_tail(units):
        """从末尾往前取句子，凑到不超过 overlap_tokens；至少留一句不带（否则下一块会和这块完全重复）。"""
        tail = []
        for unit in reversed(units[1:]):
            if tokens(tail) + unit[1] > overlap_tokens:
                break
            tail.insert(0, unit)
        return tail

    for unit in units:
        if current and tokens(current) + unit[1] > target_tokens:
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
