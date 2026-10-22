import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.failures import ErrorCode
from sqlalchemy import func

from agentnexus.services.errors import (
    ConversationArchived,
    ConversationNotFound,
    RunConflict,
    RunNotFound,
    RunNotRetryable,
)
from agentnexus.storage.postgres import AgentRun, MessageRole, RunStatus


AUTO_TITLE_MAX = 30


def auto_title(content: str) -> str | None:
    """新对话的第一条消息自动成为标题：取第一行、去空白，超长截断并加省略号。全是空白则不设标题。"""
    stripped = content.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0].strip()
    return first_line if len(first_line) <= AUTO_TITLE_MAX else first_line[:AUTO_TITLE_MAX] + "…"


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
                if conv.archived_at is not None:
                    raise ConversationArchived(str(conversation_id))
                if conv.title is None:
                    conv.title = auto_title(content)
                conv.last_activity_at = func.now()  # 与消息同一事务：列表排序与消息内容不会不一致
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

    async def active_for_conversation(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> AgentRun | None:
        """对话当前活跃的 run；没有则 None。前端刷新页面时用它找回“正在生成”的回复并重新订阅。"""
        async with self.sessionmaker() as session:
            conv = await ConversationRepository(session).get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            return await AgentRunRepository(session).get_active(conversation_id)

    async def find(self, run_id: uuid.UUID) -> AgentRun | None:
        """不做归属校验的读取，供内部使用（如 SSE 在已校验归属后轮询状态）。"""
        async with self.sessionmaker() as session:
            return await AgentRunRepository(session).get(run_id)

    async def claim(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).claim(run_id)

    async def complete(
        self, run_id: uuid.UUID, conversation_id: uuid.UUID, content: str, citations: list | None = None
    ) -> uuid.UUID | None:
        """助手回复落库 + run 置为 succeeded，同一事务，返回助手消息 id。

        run 不在 running 时整体回滚（不留孤儿消息），返回 None。
        """
        async with self.sessionmaker() as session, session.begin():
            msg = await MessageRepository(session).add(
                conversation_id, MessageRole.ASSISTANT, content, citations=citations
            )
            ok = await AgentRunRepository(session).finish(
                run_id, RunStatus.SUCCEEDED, output_message_id=msg.id
            )
            if not ok:
                await session.rollback()
                return None
            return msg.id

    async def fail(self, run_id: uuid.UUID, error: str, error_code: str = ErrorCode.INTERNAL) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(
                run_id, RunStatus.FAILED, error=error, error_code=error_code
            )

    async def cancel_queued(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).cancel_queued(run_id)

    async def cancel_running(self, run_id: uuid.UUID) -> bool:
        """running -> cancelled。与 complete/fail 一样只允许从 running 转出，先到者赢。"""
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(run_id, RunStatus.CANCELLED)

    async def list_for_conversation(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> list[AgentRun]:
        async with self.sessionmaker() as session:
            conv = await ConversationRepository(session).get(conversation_id)
            if conv is None or conv.user_id != user_id:
                raise ConversationNotFound(str(conversation_id))
            return await AgentRunRepository(session).list_by_conversation(conversation_id)

    async def retry(self, user_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        """对同一条输入消息新建一个 run，不新增用户消息（历史里不会出现重复提问）。

        只允许重试“对话的最后一条消息”：load_context 取的是截止到输入消息的历史，
        如果后面已经有别的消息，新回复会被追加到最后，顺序就乱了。
        """
        try:
            async with self.sessionmaker() as session, session.begin():
                runs = AgentRunRepository(session)
                run = await runs.get(run_id)
                conv = await ConversationRepository(session).get(run.conversation_id) if run else None
                if run is None or conv is None or conv.user_id != user_id:
                    raise RunNotFound(str(run_id))
                if conv.archived_at is not None:
                    raise ConversationArchived(str(run.conversation_id))
                if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
                    raise RunNotRetryable("only failed or cancelled runs can be retried")
                messages = await MessageRepository(session).list_by_conversation(run.conversation_id)
                if not messages or messages[-1].id != run.input_message_id:
                    raise RunNotRetryable("only the latest message can be retried")
                return await runs.create(run.conversation_id, run.input_message_id)
        except IntegrityError as exc:
            if "uq_agent_runs_one_active_per_conversation" in str(exc.orig):
                raise RunConflict(str(run_id)) from exc
            raise

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
            conversations = ConversationRepository(session)
            conv = await conversations.get(run.conversation_id)
            return RunContext(
                run_id=run.id,
                conversation_id=run.conversation_id,
                history=[ChatMessage(m.role, m.content) for m in history],
                user_id=conv.user_id,
                # 执行时读取当前的关联：用户在排队期间改了关联，以执行时为准
                knowledge_base_ids=tuple(await conversations.knowledge_base_ids(run.conversation_id)),
            )

    async def fail_orphaned_running(self, error: str, error_code: str) -> list[uuid.UUID]:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).fail_all_running(error, error_code)
