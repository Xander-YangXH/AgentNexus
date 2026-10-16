import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.services.errors import ConversationNotFound
from agentnexus.storage.postgres import Conversation, Message


class ConversationService:
    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def create(self, user_id: uuid.UUID, title: str | None) -> Conversation:
        async with self.sessionmaker() as session, session.begin():
            return await ConversationRepository(session).create(user_id, title)

    async def list_for_user(self, user_id: uuid.UUID) -> list[Conversation]:
        async with self.sessionmaker() as session:
            return await ConversationRepository(session).list_by_user(user_id)

    async def messages(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> list[Message]:
        async with self.sessionmaker() as session:
            conv = await ConversationRepository(session).get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            return await MessageRepository(session).list_by_conversation(conversation_id)
