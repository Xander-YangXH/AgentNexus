import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class UserOut(_Out):
    id: uuid.UUID
    name: str


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    """只改传了的字段。title 传 null 表示清空标题；archived 为 true/false 表示归档/恢复。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class ConversationOut(_Out):
    id: uuid.UUID
    title: str | None
    created_at: datetime
    last_activity_at: datetime
    archived_at: datetime | None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class CitationOut(BaseModel):
    """助手回答引用的片段快照（decisions/0018）。n 对应正文里的 [n]；cited=False 表示检索到了但回答里没有引用。"""

    n: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    page: int | None
    seq: int
    text: str
    vector_score: float
    rerank_score: float | None
    cited: bool


class MessageOut(_Out):
    id: uuid.UUID
    seq: int  # 分页游标：GET .../messages?before_seq=<最早一条的 seq> 取更早的
    role: str
    content: str
    citations: list[CitationOut] | None = None
    created_at: datetime


class ConversationKnowledgeBases(BaseModel):
    knowledge_base_ids: list[uuid.UUID] = Field(max_length=10)


class RunOut(_Out):
    id: uuid.UUID
    conversation_id: uuid.UUID
    input_message_id: uuid.UUID
    output_message_id: uuid.UUID | None
    status: str
    error_code: str | None  # 原始报错不对外（decisions/0009）
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ---------- 知识库（阶段 2） ----------


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class KnowledgeBaseOut(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class DocumentOut(_Out):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    error_code: str | None  # 原始报错不对外
    chunk_count: int
    created_at: datetime
    processed_at: datetime | None


class SearchRequest(BaseModel):
    knowledge_base_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    rerank: bool = True


class SearchHitOut(_Out):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    seq: int
    page: int | None
    text: str
    vector_score: float
    rerank_score: float | None


class SearchOut(_Out):
    hits: list[SearchHitOut]
    reranked: bool  # 部署关闭了重排时，即使请求 rerank=true 也是 false
    timings_ms: dict[str, float]


class ChunkOut(_Out):
    id: uuid.UUID
    seq: int
    text: str
    token_count: int
    page: int | None


# ---------- 知识图谱（decisions/0019） ----------


class GraphEstimateOut(BaseModel):
    chunk_count: int  # 参与构建的分块（处理完成的文档）
    unique_count: int  # 按内容去重后
    cached_count: int  # 其中已有抽取结果（不再花钱）
    extract_count: int  # 需要调用模型的数量
    estimated_cost_usd: float
    max_extract_count: int  # 单次构建的上限


class GraphBuildOut(_Out):
    id: uuid.UUID
    status: str
    chunk_count: int
    unique_count: int
    extract_count: int
    estimated_cost_usd: float
    done_count: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float
    entity_count: int
    relation_count: int
    error_code: str | None  # 原始报错不对外
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class GraphNode(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    mentions: int


class GraphEdge(BaseModel):
    id: uuid.UUID
    source: uuid.UUID
    target: uuid.UUID
    predicate: str
    weight: int


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class EntityRelationOut(BaseModel):
    id: uuid.UUID
    direction: str  # out：本实体是起点；in：本实体是终点
    predicate: str
    description: str
    other_id: uuid.UUID
    other_name: str
    mentions: int


class EntityMentionOut(BaseModel):
    chunk_id: uuid.UUID
    filename: str
    page: int | None
    seq: int
    text: str


class EntityDetailOut(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    name: str
    type: str
    description: str
    mention_count: int
    relations: list[EntityRelationOut]
    mentions: list[EntityMentionOut]
