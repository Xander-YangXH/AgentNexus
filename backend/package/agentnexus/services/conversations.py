import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.services.errors import ConversationNotFound
from agentnexus.storage.postgres import Conversation, Message

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
