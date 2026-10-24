"""关键词检索的稀疏向量（阶段 7.5，decisions/0029）：与向量检索融合，补上“精确词”查询。

实验（eval/eval_keyword_queries.py）：语料加入 713 段技术文档作为干扰后，只含精确词的查询（“429”“CSV”“72 小时”）
有 30% 的正确答案连向量召回的前 10 个候选都进不去，重排也救不回来；与关键词检索按排名融合（RRF）后候选召回 1.00。

切词：不做中文分词（分词词典与领域相关，还要额外依赖），中文按字二元组，英文和数字按连续的字母数字串，
先做 NFKC（全角转半角）并转小写。词用 CRC32 映射成稀疏向量的维度（冲突的词合并计数，概率很小）。
权重：文档侧用 BM25 的词频饱和（k1、b、按固定的平均长度做长度归一），IDF 交给 Qdrant 在服务端按整个集合计算
（稀疏向量的 modifier=IDF），查询侧每个词权重 1。两者的点积就是 BM25 的近似。
"""

import dataclasses
import re
import unicodedata
import zlib
from collections import Counter

# 中文（基本区、扩展 A、兼容区）连续成段；英文和数字连续成词
_TOKEN = re.compile(r"[a-z0-9]+|[㐀-䶿一-鿿豈-﫿]+")
K1, B = 1.2, 0.75
AVG_TOKENS = 200.0  # 分块上限 480 个模型 token，中文字二元组的个数与它同量级；只影响长度归一的力度


@dataclasses.dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


def tokens(text: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(unicodedata.normalize("NFKC", text).lower()):
        run = m.group()
        if run.isascii() or len(run) == 1:  # 英文 / 数字整词；单个汉字原样
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _ids(toks: list[str]) -> Counter:
    return Counter(zlib.crc32(t.encode()) for t in toks)


def document_vector(text: str) -> SparseVector:
    toks = tokens(text)
    counts = _ids(toks)
    norm = K1 * (1 - B + B * len(toks) / AVG_TOKENS)
    ids = sorted(counts)
    return SparseVector(ids, [counts[i] * (K1 + 1) / (counts[i] + norm) for i in ids])


def query_vector(text: str) -> SparseVector:
    ids = sorted(_ids(tokens(text)))
    return SparseVector(ids, [1.0] * len(ids))
