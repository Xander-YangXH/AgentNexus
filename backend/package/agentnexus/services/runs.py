import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.services.errors import ConversationNotFound, RunConflict, RunNotFound
from agentnexus.storage.postgres import AgentRun, MessageRole, RunStatus


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

    async def get(self, user_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        """按 run 所属对话校验归属：不是自己的 run 与不存在同样报 RunNotFound。"""
        async with self.sessionmaker() as session:
            run = await AgentRunRepository(session).get(run_id)
            if run is None:
                raise RunNotFound(str(run_id))
            conv = await ConversationRepository(session).get(run.conversation_id)
            if conv is None or conv.user_id != user_id:
                raise RunNotFound(str(run_id))
            return run

    async def find(self, run_id: uuid.UUID) -> AgentRun | None:
        """不做归属校验的读取，供内部使用（如 SSE 在已校验归属后轮询状态）。"""
        async with self.sessionmaker() as session:
            return await AgentRunRepository(session).get(run_id)

    async def claim(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).claim(run_id)

    async def complete(
        self, run_id: uuid.UUID, conversation_id: uuid.UUID, content: str
    ) -> uuid.UUID | None:
        """助手回复落库 + run 置为 succeeded，同一事务，返回助手消息 id。

        run 不在 running 时整体回滚（不留孤儿消息），返回 None。
        """
        async with self.sessionmaker() as session, session.begin():
            msg = await MessageRepository(session).add(conversation_id, MessageRole.ASSISTANT, content)
            ok = await AgentRunRepository(session).finish(
                run_id, RunStatus.SUCCEEDED, output_message_id=msg.id
            )
            if not ok:
                await session.rollback()
                return None
            return msg.id

    async def fail(self, run_id: uuid.UUID, error: str) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(run_id, RunStatus.FAILED, error=error)

    async def list_queued_ids(self, limit: int = 1000) -> list[uuid.UUID]:
        async with self.sessionmaker() as session:
            return [r.id for r in await AgentRunRepository(session).list_queued(limit)]

    async def load_context(self, run_id: uuid.UUID) -> RunContext:
        """取该 run 的输入上下文：对话历史截止到本次输入消息（含），保证重放结果稳定。"""
        async with self.sessionmaker() as session:
            run = await AgentRunRepository(session).get(run_id)
            if run is None:
                raise RunNotFound(str(run_id))
            messages = MessageRepository(session)
            input_msg = await messages.get(run.input_message_id)
            history = await messages.list_by_conversation(run.conversation_id, up_to_seq=input_msg.seq)
            return RunContext(
                run_id=run.id,
                conversation_id=run.conversation_id,
                history=[ChatMessage(m.role, m.content) for m in history],
            )

    async def fail_orphaned_running(self, error: str) -> list[uuid.UUID]:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).fail_all_running(error)
