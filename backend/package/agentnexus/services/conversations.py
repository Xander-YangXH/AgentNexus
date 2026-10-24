import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.runtime.lease import notify_jobs
from agentnexus.services.errors import ConversationNotFound, KnowledgeBaseNotFound, RunConflict
from agentnexus.security import access
from agentnexus.storage.postgres import (
    ACTIVE_STATUSES,
    AgentRun,
    Artifact,
    Conversation,
    Message,
    PurgeJob,
    PurgeKind,
)

_UNSET = object()


class ConversationService:
    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def create(self, user_id: uuid.UUID, title: str | None, agent: str = "chat") -> Conversation:
        async with self.sessionmaker() as session, session.begin():
            return await ConversationRepository(session).create(user_id, title, agent)

    async def list_for_user(self, user_id: uuid.UUID, *, archived: bool = False) -> list[Conversation]:
        async with self.sessionmaker() as session:
            return await ConversationRepository(session).list_by_user(user_id, archived=archived)

    async def update(self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, title=_UNSET, archived=None) -> Conversation:
        """改标题、归档 / 恢复。title=None 表示清空标题（由前端回退显示创建时间）；不传表示不改。"""
        async with self.sessionmaker() as session, session.begin():
            conv = await ConversationRepository(session).get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            if title is not _UNSET:
                conv.title = title
            if archived is not None:
                # 重复归档不改时间：保留“第一次归档”的时间点
                if archived and conv.archived_at is None:
                    conv.archived_at = datetime.now(UTC)
                elif not archived:
                    conv.archived_at = None
            return conv

    async def delete(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        """删除对话（decisions/0028）：消息、运行记录、产物在这个事务里删掉；产物原件、沙盒工作空间、
        LangGraph checkpoint 由清理任务删（0026 的发件箱）。用量明细保留（不含内容，只有数字，用于计费和统计）。

        有进行中的 run（排队、执行、等待审批）时拒绝：先停止。锁住对话行，与“发新消息”串行——
        新的 run、消息对对话有外键，插入时要拿这一行的共享锁。
        """
        async with self.sessionmaker() as session, session.begin():
            conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            active = exists().where(AgentRun.conversation_id == conversation_id, AgentRun.status.in_(ACTIVE_STATUSES))
            if await session.scalar(select(active)):
                raise RunConflict(str(conversation_id))
            run_ids = [str(r) for r in (await session.scalars(select(AgentRun.id).where(
                AgentRun.conversation_id == conversation_id, AgentRun.checkpoint_purged_at.is_(None)))).all()]
            blob_keys = list((await session.scalars(delete(Artifact).where(
                Artifact.conversation_id == conversation_id).returning(Artifact.blob_key))).all())
            await session.execute(delete(AgentRun).where(AgentRun.conversation_id == conversation_id))
            await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await session.execute(delete(Conversation).where(Conversation.id == conversation_id))  # 关联知识库级联删除
            session.add(PurgeJob(kind=PurgeKind.CONVERSATION, conversation_id=conversation_id, blob_keys=blob_keys,
                                 run_ids=run_ids))
            await notify_jobs(session, "purge")

    async def messages(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, before_seq: int | None = None, limit: int = 100
    ) -> tuple[list[Message], bool]:
        async with self.sessionmaker() as session:
            conv = await ConversationRepository(session).get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            return await MessageRepository(session).list_page(conversation_id, before_seq=before_seq, limit=limit)

    async def knowledge_bases(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> list[uuid.UUID]:
        async with self.sessionmaker() as session:
            repo = ConversationRepository(session)
            conv = await repo.get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            return await repo.knowledge_base_ids(conversation_id)

    async def set_knowledge_bases(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, kb_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """设置对话关联的知识库（整体替换）。对话必须属于该用户；知识库是自己的或同部门共享的（decisions/0023）。"""
        kb_ids = list(dict.fromkeys(kb_ids))
        async with self.sessionmaker() as session, session.begin():
            repo = ConversationRepository(session)
            conv = await repo.get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            if kb_ids:
                visible = set(await access.visible_kb_ids(session, user_id, kb_ids))
                missing = [k for k in kb_ids if k not in visible]
                if missing:
                    raise KnowledgeBaseNotFound(str(missing[0]))
            await repo.set_knowledge_bases(conversation_id, kb_ids)
            return await repo.knowledge_base_ids(conversation_id)
