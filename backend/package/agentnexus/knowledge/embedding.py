"""文本向量化（decisions/0014）：本地 bge-small-zh-v1.5，经 fastembed 用 ONNX 在 CPU 上运行，零 API 费用。

接口是同步的（纯 CPU 计算），调用方用 asyncio.to_thread 放到线程里，避免阻塞事件循环。
"""

from collections.abc import Sequence
from typing import Protocol

from agentnexus.knowledge.tokens import TokenizerCounter

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    """加载较慢（读模型文件、建 ONNX 会话），整个进程只建一个。

    模型文件在构建镜像时下载好（见 docker/api.Dockerfile），运行时不联网。
    查询不加 bge 官方推荐的前缀：在自建评测集上加了反而变差（decisions/0014）。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, *, batch_size: int = 32):
        from fastembed import TextEmbedding  # 延迟导入：只用到分块、不用向量的地方（如单元测试）不必加载 onnxruntime

        self._model = TextEmbedding(model_name)
        self._batch_size = batch_size
        self.model_name = model_name
        self.dim: int = self._model.embedding_size

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        # fastembed 输出已归一化（长度为 1），余弦相似度等于点积
        return [v.tolist() for v in self._model.embed(list(texts), batch_size=self._batch_size)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def token_counter(self) -> TokenizerCounter:
        """与模型实际看到的一致的 token 计数器（分块用）。"""
        return TokenizerCounter(self._model.model.tokenizer)
