"""重排（decisions/0014、0017）：召回的候选用 cross-encoder 逐个与问题一起打分，再按分数重新排序。

cross-encoder 同时读问题和片段，比“各自编码再比相似度”的向量召回准，但慢：每个候选都要过一遍模型，
所以只对召回的前 N 个（默认 10）重排。实测（Mac CPU）：10 个约 200 字的候选约 1 秒，耗时与候选总长度近似成正比。
接口是同步的（纯 CPU 计算），调用方放到线程里。
"""

from collections.abc import Sequence
from typing import Protocol

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3-int8"


class Reranker(Protocol):
    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class FastReranker:
    """bge-reranker-v2-m3 的 int8 量化版（0.57GB，加载后进程内存约多 0.8GB）。模型在构建镜像时下载。"""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model_name)
        self.model_name = model_name

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        # 分数是模型的原始输出（logit），只用于同一个问题内部排序，不同问题之间不可比
        return [float(s) for s in self._model.rerank(query, list(texts))]
