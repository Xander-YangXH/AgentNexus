"""检索结果的领域类型。单独放在这里：Agent（工具）要用它，但不应该因此依赖服务层和数据库（agents/types.py 的约定）。"""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    seq: int
    page: int | None
    text: str
    vector_score: float  # 余弦相似度，-1~1
    rerank_score: float | None  # 重排模型的原始分数，只在同一个问题内部可比
