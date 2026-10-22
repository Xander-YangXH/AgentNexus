"""测试用的替身。"""


class FakeEmbedder:
    """确定性的假向量：按字的二元组哈希到固定维度再归一化。字面越像的文本越相近，足够测试检索流程，不加载模型。"""

    dim = 64

    def embed_documents(self, texts):
        return [self._one(t) for t in texts]

    def embed_query(self, text):
        return self._one(text)

    def _one(self, text):
        import hashlib
        import math

        v = [0.0] * self.dim
        grams = [text[i:i + 2] for i in range(max(1, len(text) - 1))]
        for g in grams:
            v[int(hashlib.md5(g.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


# 是否加载过真实的 ONNX 模型（fastembed）。见 conftest.py 的 pytest_unconfigure
NATIVE_MODELS = {"loaded": False}
