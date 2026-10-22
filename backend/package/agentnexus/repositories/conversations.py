import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import Conversation, ConversationKnowledgeBase, Message, MessageRole


def conversations_list_stmt(user_id: uuid.UUID, *, archived: bool = False):
    # 未归档时必须带 archived_at IS NULL，规划器才能用部分索引 ix_conversations_user_recent_active；
    # 抽成函数是为了让测试对“生产代码真正执行的这条查询”做 EXPLAIN，而不是测试里另写一条
    stmt = select(Conversation).where(
        Conversation.user_id == user_id,
        Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None),
    )
    return stmt.order_by(Conversation.last_activity_at.desc(), Conversation.id)


class ConversationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: uuid.UUID, title: str | None = None, agent: str = "chat") -> Conversation:
        conv = Conversation(user_id=user_id, title=title, agent=agent)
        self.session.add(conv)
        await self.session.flush()
        return conv

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def list_by_user(self, user_id: uuid.UUID, *, archived: bool = False) -> list[Conversation]:
        return list((await self.session.scalars(conversations_list_stmt(user_id, archived=archived))).all())

    async def knowledge_base_ids(self, conversation_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(ConversationKnowledgeBase.knowledge_base_id)
            .where(ConversationKnowledgeBase.conversation_id == conversation_id)
            .order_by(ConversationKnowledgeBase.created_at, ConversationKnowledgeBase.knowledge_base_id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def set_knowledge_bases(self, conversation_id: uuid.UUID, kb_ids: list[uuid.UUID]) -> None:
        """整体替换（先删后插）：关联最多几十条，比逐条比对增删简单，也不会留下半套。"""
        await self.session.execute(
            delete(ConversationKnowledgeBase).where(ConversationKnowledgeBase.conversation_id == conversation_id)
        )
        self.session.add_all(ConversationKnowledgeBase(conversation_id=conversation_id, knowledge_base_id=k) for k in kb_ids)
        await self.session.flush()


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self, conversation_id: uuid.UUID, role: MessageRole, content: str, *, citations: list | None = None,
        attachments: list | None = None,
    ) -> Message:
        msg = Message(conversation_id=conversation_id, role=role, content=content, citations=citations,
                      attachments=attachments)
        self.session.add(msg)
        await self.session.flush()  # 触发 INSERT，拿到 id / seq
        return msg

    async def get(self, message_id: uuid.UUID) -> Message | None:
        return await self.session.get(Message, message_id)

    async def list_page(
        self, conversation_id: uuid.UUID, *, before_seq: int | None, limit: int
    ) -> tuple[list[Message], bool]:
        """游标分页：取 before_seq 之前（不含）的最近 limit 条，按时间正序返回，并告知是否还有更早的。

        多取一条用来判断 has_more，省去一次 count 查询。走 (conversation_id, seq) 索引，翻到多早都一样快。
        """
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before_seq is not None:
            stmt = stmt.where(Message.seq < before_seq)
        rows = list((await self.session.scalars(stmt.order_by(Message.seq.desc()).limit(limit + 1))).all())
        has_more = len(rows) > limit
        return list(reversed(rows[:limit])), has_more

    async def list_by_conversation(
        self, conversation_id: uuid.UUID, up_to_seq: int | None = None
    ) -> list[Message]:
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if up_to_seq is not None:
            stmt = stmt.where(Message.seq <= up_to_seq)
        stmt = stmt.order_by(Message.seq)
        return list((await self.session.scalars(stmt)).all())
