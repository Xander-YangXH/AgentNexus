import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.services.errors import ConversationNotFound, KnowledgeBaseNotFound
from agentnexus.storage.postgres import Conversation, KnowledgeBase, Message

_UNSET = object()


class ConversationService:
    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def create(self, user_id: uuid.UUID, title: str | None) -> Conversation:
        async with self.sessionmaker() as session, session.begin():
            return await ConversationRepository(session).create(user_id, title)

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
        """设置对话关联的知识库（整体替换）。对话和每个知识库都必须属于该用户。"""
        kb_ids = list(dict.fromkeys(kb_ids))
        async with self.sessionmaker() as session, session.begin():
            repo = ConversationRepository(session)
            conv = await repo.get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            if kb_ids:
                stmt = select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids), KnowledgeBase.user_id == user_id)
                owned = set((await session.scalars(stmt)).all())
                missing = [k for k in kb_ids if k not in owned]
                if missing:
                    raise KnowledgeBaseNotFound(str(missing[0]))
            await repo.set_knowledge_bases(conversation_id, kb_ids)
            return await repo.knowledge_base_ids(conversation_id)
