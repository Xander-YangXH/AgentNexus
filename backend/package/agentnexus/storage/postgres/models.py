import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Integer,
    UniqueConstraint,
    CheckConstraint,
    DateTime,
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


ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)

def _uuid_pk():
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _in_list(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    """对话。历史消息的事实来源是 messages 表；LangGraph 的 thread_id 用 run_id，不用本表 id（见 decisions/0006）。"""

    __tablename__ = "conversations"
    __table_args__ = (
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
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
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(Text)  # 原始报错，只供排查，不对外
    error_code: Mapped[str | None] = mapped_column(String(32))  # 对外的错误码（runtime/failures.py）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 该 run 的 LangGraph checkpoint 何时被清理（decisions/0010）；用我们自己的列记账，不去查 LangGraph 的内部表
    checkpoint_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------- 知识库（阶段 2，decisions/0014、0015） ----------------


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"  # 已上传，等待处理
    PROCESSING = "processing"  # 正在解析 / 分块（2.2 起还包括计算向量、写入 Qdrant）
    READY = "ready"
    FAILED = "failed"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # 阶段 5 前按用户归属；之后改为部门 / 角色可见性
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
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
