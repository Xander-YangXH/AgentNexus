"""token 计数。分块大小必须按 embedding 模型自己的分词器来算，否则可能超过模型的输入上限而被截断。

运行时用 TokenizerCounter（bge-small-zh 自带的分词器）；EstimatingCounter 是不加载模型时的近似（单元测试、离线工具）。
"""

import re
from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z0-9]+")


class TokenizerCounter:
    """用 embedding 模型自己的分词器计数。

    复制一份再关掉截断和补齐：fastembed 内部那份开着“截断到 512”，拿它来数，超长文本永远只数出 512，
    分块的“强制切开超长句子”就失效了；另外分块在后台线程里调用，不与向量化共用同一个分词器对象。
    不计 [CLS] / [SEP] 这类特殊 token：分块的硬上限（480）已经给它们留了余量。
    """

    def __init__(self, tokenizer):
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_str(tokenizer.to_str())
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)


class EstimatingCounter:
    """估算：每个汉字 1 个 token（与 BERT 中文分词一致），英文 / 数字按每个词约 1.3 个 token，标点各算 1 个。"""

    def count(self, text: str) -> int:
        cjk = len(_CJK.findall(text))
        words = _WORD.findall(text)
        rest = _CJK.sub("", _WORD.sub("", text))
        punct = sum(1 for ch in rest if not ch.isspace())
        return cjk + round(sum(1.3 for _ in words)) + punct
