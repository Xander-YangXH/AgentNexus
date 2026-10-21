import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import UUID
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
