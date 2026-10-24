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
    # 召回阶段的分数：混合检索（0029）时是 RRF 排名分（越大越靠前），只用稠密向量时是余弦相似度。
    # 字段名沿用：引用快照里已经存了这个名字
    vector_score: float
    rerank_score: float | None  # 重排模型的原始分数，只在同一个问题内部可比
