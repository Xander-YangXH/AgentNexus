"""token 计数。分块大小必须按 embedding 模型自己的分词器来算，否则可能超过模型的输入上限而被截断。

阶段 2.1 先用估算器；2.2 接入 bge-small-zh 时换成它自带的分词器（BertTokenizer：中文每个字一个 token）。
"""

import re
from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z0-9]+")


class EstimatingCounter:
    """估算：每个汉字 1 个 token（与 BERT 中文分词一致），英文 / 数字按每个词约 1.3 个 token，标点各算 1 个。"""

    def count(self, text: str) -> int:
        cjk = len(_CJK.findall(text))
        words = _WORD.findall(text)
        rest = _CJK.sub("", _WORD.sub("", text))
        punct = sum(1 for ch in rest if not ch.isspace())
        return cjk + round(sum(1.3 for _ in words)) + punct
