import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.storage.postgres import AgentRun, MessageRole, RunStatus


class ConversationNotFound(Exception):
    pass


class RunConflict(Exception):
    """该对话已有未结束的 run。"""


class RunService:
    """每个方法一个事务，边界清晰：成功即全部提交，失败即全部回滚。"""

    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def submit(self, user_id: uuid.UUID, conversation_id: uuid.UUID, content: str) -> AgentRun:
        """用户消息 + queued 的 AgentRun 在同一事务里写入。

        返回即代表"一定会被执行"：之后投递队列失败也没关系，补投递扫描会捡到这条记录。
        """
        try:
            async with self.sessionmaker() as session, session.begin():
                conv = await ConversationRepository(session).get(conversation_id)
                # 归属校验放在服务层：查不到和不属于自己对外同一个错误，不泄露 id 是否存在
                if conv is None or conv.user_id != user_id:
                    raise ConversationNotFound(str(conversation_id))
                msg = await MessageRepository(session).add(conversation_id, MessageRole.USER, content)
                return await AgentRunRepository(session).create(conversation_id, msg.id)
        except IntegrityError as exc:
            if "uq_agent_runs_one_active_per_conversation" in str(exc.orig):
                raise RunConflict(str(conversation_id)) from exc
            raise

    async def claim(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).claim(run_id)

    async def complete(self, run_id: uuid.UUID, conversation_id: uuid.UUID, content: str) -> bool:
        """助手回复落库 + run 置为 succeeded，同一事务。run 不在 running 时整体回滚，不留孤儿消息。"""
        async with self.sessionmaker() as session, session.begin():
            msg = await MessageRepository(session).add(conversation_id, MessageRole.ASSISTANT, content)
            ok = await AgentRunRepository(session).finish(
                run_id, RunStatus.SUCCEEDED, output_message_id=msg.id
            )
            if not ok:
                await session.rollback()
            return ok

    async def fail(self, run_id: uuid.UUID, error: str) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(run_id, RunStatus.FAILED, error=error)
