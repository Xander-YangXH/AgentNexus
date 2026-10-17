import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import Conversation, Message, MessageRole


class ConversationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: uuid.UUID, title: str | None = None) -> Conversation:
        conv = Conversation(user_id=user_id, title=title)
        self.session.add(conv)
        await self.session.flush()
        return conv

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def list_by_user(self, user_id: uuid.UUID) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, conversation_id: uuid.UUID, role: MessageRole, content: str) -> Message:
        msg = Message(conversation_id=conversation_id, role=role, content=content)
        self.session.add(msg)
        await self.session.flush()  # 触发 INSERT，拿到 id / seq
        return msg

    async def get(self, message_id: uuid.UUID) -> Message | None:
        return await self.session.get(Message, message_id)

    async def list_by_conversation(
        self, conversation_id: uuid.UUID, up_to_seq: int | None = None
    ) -> list[Message]:
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if up_to_seq is not None:
            stmt = stmt.where(Message.seq <= up_to_seq)
        stmt = stmt.order_by(Message.seq)
        return list((await self.session.scalars(stmt)).all())
