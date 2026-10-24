import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Integer,
    UniqueConstraint,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 等待用户审批（decisions/0022）：这次执行已结束、租约已放弃，不占 worker；批准后回到 queued，从 checkpoint 继续
    AWAITING_APPROVAL = "awaiting_approval"


# 活跃 = 还会继续写这个 run 的 checkpoint。等待审批也算：同一对话不能在等待期间再开一个 run
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL)


class AgentKind(enum.StrEnum):
    """对话使用哪种 Agent（decisions/0022）：建对话时选定。"""

    CHAT = "chat"
    RESEARCH = "research"  # 深度调研：制定计划 → 审批 → 并行子 Agent → 汇总成报告

def _uuid_pk():
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _in_list(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


class LeaseMixin:
    """任务租约（decisions/0020）：执行中的 worker 定期续约；租约过期即可被别的 worker 接管。

    - lease_version 是 fencing token：每次认领加一，执行中的写入都带上它校验，被接管后旧 worker 的写入一定失败；
    - attempts：被认领执行的次数（含接管），超过上限就不再执行，标为失败。
    """

    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class Role(enum.StrEnum):
    """固定角色（decisions/0023）。不做可配置的角色—权限表：演示和需求都用不到。"""

    ADMIN = "admin"  # 管理用户和部门，看全站用量
    MANAGER = "manager"  # 部门负责人：看本部门用量
    MEMBER = "member"


class Visibility(enum.StrEnum):
    PRIVATE = "private"  # 只有所有者可见
    DEPARTMENT = "department"  # 同部门的人只读可见


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in_list("role", Role), name="ck_users_role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 登录（decisions/0023）：argon2id 哈希。为空 = 还没设置密码，不能登录（升级前就存在的用户）
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER, server_default=Role.MEMBER.value)
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"), index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 禁用即刻生效：会话校验时检查


class UserSession(Base):
    """登录会话（decisions/0023）。只存令牌的 SHA-256：库泄露也拿不到能用的令牌。退出 = 删除这一行，即刻生效。"""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    """对话。历史消息的事实来源是 messages 表；LangGraph 的 thread_id 用 run_id，不用本表 id（见 decisions/0006）。"""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_in_list("agent", AgentKind), name="ck_conversations_agent"),
        Index(
            "ix_conversations_user_recent_active",
            "user_id",
            text("last_activity_at DESC"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    agent: Mapped[str] = mapped_column(String(16), default=AgentKind.CHAT, server_default=AgentKind.CHAT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 归档而不是删除（decisions/0012）：隐藏、可恢复；真正删除要连带清理消息、run、checkpoint、事件流
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 最近活动时间：用户发消息时在同一事务里更新，对话列表按它倒序
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_in_list("role", MessageRole), name="ck_messages_role"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # 自增序号用于排序：同一事务内 now() 相同，uuid 无序，都不能保证消息顺序
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # 助手回答引用的知识库片段（decisions/0018）。存快照而不是只存分块 id：文档以后重新处理、分块被替换，
    # 历史回答的引用仍然能展示当时的原文。没有检索的回答为 NULL
    citations: Mapped[list | None] = mapped_column(JSONB)
    # 附件（decisions/0022）：产物的快照 [{id, filename, content_type, size}]，下载走 /api/artifacts/{id}
    attachments: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(LeaseMixin, Base):
    """一次 Agent 执行。提交时与用户消息同事务写入（status=queued），队列只是触发器，库才是事实来源。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(_in_list("status", RunStatus), name="ck_agent_runs_status"),
        # 不变量：同一对话同一时刻最多一个活跃 run，避免两个 run 并发写同一个 thread 的 checkpoint
        Index(
            "uq_agent_runs_one_active_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(_in_list("status", ACTIVE_STATUSES)),
        ),
        Index("ix_agent_runs_status", "status"),
        Index(
            "ix_agent_runs_checkpoint_purge_candidates",
            "finished_at",
            postgresql_where=text("finished_at IS NOT NULL AND checkpoint_purged_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    input_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"))
    output_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(24), default=RunStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(Text)  # 原始报错，只供排查，不对外
    error_code: Mapped[str | None] = mapped_column(String(32))  # 对外的错误码（runtime/failures.py）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 该 run 的 LangGraph checkpoint 何时被清理（decisions/0010）；用我们自己的列记账，不去查 LangGraph 的内部表
    checkpoint_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 取消请求（跨进程）：取消接口写这里（事实来源）并发一条 Redis 消息；执行它的 worker 收到消息或在心跳时发现后停止
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 审批（decisions/0022）：等待时展示给用户的内容（例如调研计划）与用户的答复（恢复执行时交给图）
    approval_request: Mapped[dict | None] = mapped_column(JSONB)
    approval_response: Mapped[dict | None] = mapped_column(JSONB)
    approval_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    """Agent 产出的文件（decisions/0022），例如调研报告。内容放在原件存储（blob_key），这里只存元数据。

    与助手消息在同一事务里写入（带租约版本校验）：被接管的旧执行者写不进来。
    """

    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_conversation", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # 由 Agent 生成：先写原件，再落库
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    blob_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------- 知识库（阶段 2，decisions/0014、0015） ----------------


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"  # 已上传，等待处理
    PROCESSING = "processing"  # 正在解析 / 分块（2.2 起还包括计算向量、写入 Qdrant）
    READY = "ready"
    FAILED = "failed"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),
        CheckConstraint(_in_list("visibility", Visibility), name="ck_knowledge_bases_visibility"),
        Index("ix_knowledge_bases_department_shared", "department_id",
              postgresql_where=text("visibility = 'department'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)  # 所有者：只有他能修改
    # 可见性（decisions/0023）：department 时同部门的人只读可见。部门在设为共享时记下（所有者当时所在的部门）
    visibility: Mapped[str] = mapped_column(String(16), default=Visibility.PRIVATE, server_default=Visibility.PRIVATE.value)
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(LeaseMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in_list("status", DocumentStatus), name="ck_documents_status"),
        # 同一知识库里同一份文件（按内容哈希）只存一次，避免检索时同一段内容出现两遍
        UniqueConstraint("knowledge_base_id", "sha256", name="uq_documents_kb_sha256"),
        Index("ix_documents_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_documents_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # 由应用生成：写原件时就要用到 id
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id"))
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    blob_key: Mapped[str] = mapped_column(String(300))  # 原件在文件存储里的 key
    status: Mapped[str] = mapped_column(String(16), default=DocumentStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)  # 原始报错，只供排查
    error_code: Mapped[str | None] = mapped_column(String(32))  # 对外的错误码
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    """分块是检索的事实来源（含原文）；Qdrant 只是从这里派生出来的向量索引，可以随时重建（decisions/0014）。"""

    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "seq", name="uq_document_chunks_doc_seq"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # 冗余存一份知识库 id：检索时按知识库过滤，不必再连表
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)  # PDF 页码，用于标注引用出处
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationKnowledgeBase(Base):
    """对话关联的知识库（decisions/0018）。Agent 检索哪些知识库只由这里和对话归属决定，模型不能自己指定。"""

    __tablename__ = "conversation_knowledge_bases"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 知识图谱（decisions/0019）：PostgreSQL 是事实来源，Neo4j 是派生的图查询存储 ----------


class GraphBuildStatus(enum.StrEnum):
    PENDING = "pending"  # 已创建，等待后台执行
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GraphBuild(LeaseMixin, Base):
    """一次“构建图谱”：抽取（有缓存）→ 合并写入 PostgreSQL → 同步到 Neo4j。沿用“先落库再投递 + 原子抢占”。"""

    __tablename__ = "kg_builds"
    __table_args__ = (
        CheckConstraint(_in_list("status", GraphBuildStatus), name="ck_kg_builds_status"),
        # 同一知识库同一时刻最多一个进行中的构建（两次构建同时重写同一张图会互相覆盖）
        Index(
            "uq_kg_builds_one_active_per_kb",
            "knowledge_base_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_kg_builds_kb_created", "knowledge_base_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=GraphBuildStatus.PENDING)
    # 创建时的预估：参与构建的分块数、去重后的文本数、其中需要调用模型的数量（其余命中抽取缓存）、预估费用
    chunk_count: Mapped[int] = mapped_column(Integer)
    unique_count: Mapped[int] = mapped_column(Integer)
    extract_count: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float] = mapped_column(Float)
    # 执行中 / 执行后的实际值
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)  # 原始报错，只进数据库
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphExtraction(Base):
    """抽取缓存：同一知识库里内容相同的分块只调用一次模型；提示词改版（prompt_version 变化）后自然失效。

    构建失败后重试、或文档没变时再次构建，都不再花钱。
    """

    __tablename__ = "kg_extractions"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    text_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)  # {"entities": [...], "relations": [...]}
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphEntity(Base):
    __tablename__ = "kg_entities"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "key", name="uq_kg_entities_kb_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(200))  # 规范化名称：合并的依据（全角转半角、去空白、英文小写）
    name: Mapped[str] = mapped_column(String(200))  # 显示名称：出现最多的写法
    type: Mapped[str] = mapped_column(String(32))  # 多数投票
    description: Mapped[str] = mapped_column(Text)
    mention_count: Mapped[int] = mapped_column(Integer)


class GraphRelation(Base):
    __tablename__ = "kg_relations"
    __table_args__ = (UniqueConstraint("source_id", "target_id", "predicate", name="uq_kg_relations_edge"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # 重建时按知识库整体删除，需要索引
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kg_entities.id", ondelete="CASCADE"))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True)
    predicate: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    mention_count: Mapped[int] = mapped_column(Integer)


class GraphMention(Base):
    """溯源：实体 / 关系出自哪些分块。点开图上的节点时据此展示原文。"""

    __tablename__ = "kg_mentions"
    __table_args__ = (
        CheckConstraint("(entity_id IS NULL) <> (relation_id IS NULL)", name="ck_kg_mentions_one_target"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    # 删除分块（文档重新处理）时级联删除提及：没有索引就要扫全表
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_chunks.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True)
    relation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kg_relations.id", ondelete="CASCADE"), index=True)


# ---------------- 删除后的清理（阶段 7.1，decisions/0026） ----------------


class PurgeKind(enum.StrEnum):
    DOCUMENT = "document"
    KNOWLEDGE_BASE = "knowledge_base"
    CONVERSATION = "conversation"  # 阶段 7.3（decisions/0028）


class PurgeStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"


class PurgeJob(LeaseMixin, Base):
    """删除的“发件箱”：删除 PostgreSQL 里的数据时，在同一事务里插入一条清理任务，由 worker 清理派生副本
    （Qdrant 的点、原件、Neo4j 的图）。事务提交 = 数据已删除且清理一定会发生；清理失败就退避重试，成功后删掉这一行。

    知识库 / 文档的行已经删了，所以不加外键；清理需要的信息（原件的 key）记在这里。
    """

    __tablename__ = "purge_jobs"
    __table_args__ = (
        CheckConstraint(_in_list("kind", PurgeKind), name="ck_purge_jobs_kind"),
        CheckConstraint(_in_list("status", PurgeStatus), name="ck_purge_jobs_status"),
        Index("ix_purge_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(16))
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 删除单篇文档时
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 删除对话时：删它的沙盒工作空间
    blob_keys: Mapped[list] = mapped_column(JSONB)
    # 删除对话时：还没被清理的 checkpoint（thread_id = run_id）
    run_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), default=PurgeStatus.PENDING, server_default=PurgeStatus.PENDING.value)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 失败后退避：这之前不认领
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------- 用量（阶段 5，decisions/0023） ----------------


class UsageKind(enum.StrEnum):
    RUN = "run"  # 一次对话 / 调研请求（提交时记）
    LLM = "llm"  # 一次模型调用（回调在调用结束时记，崩溃前已花的也记得到）
    SEARCH = "search"  # 一次知识库检索


class UsageEvent(Base):
    """用量明细：每次请求 / 模型调用 / 检索一行，Dashboard 查询时按天、按小时聚合（实验：百万行时个人 / 部门 5~21ms）。

    记下当时所在的部门：用户换部门后，历史用量仍算在原部门。
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(_in_list("kind", UsageKind), name="ck_usage_events_kind"),
        Index("ix_usage_events_user_time", "user_id", "created_at"),
        Index("ix_usage_events_department_time", "department_id", "created_at"),
        Index("ix_usage_events_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))  # chat / research / graph / search_page
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 不加外键：明细不随 run 删除
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

