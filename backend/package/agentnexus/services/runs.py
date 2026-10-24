import dataclasses
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.failures import ErrorCode
from agentnexus.runtime.lease import notify_jobs
from sqlalchemy import func, select, text

from agentnexus.services.errors import (
    ConversationArchived,
    ConversationNotFound,
    QuotaExceeded,
    RunConflict,
    RunNotAwaitingApproval,
    RunNotFound,
    RunNotRetryable,
)
from agentnexus.security import access
from agentnexus.services.usage import usage_insert
from agentnexus.storage.postgres import AgentRun, Artifact, MessageRole, Role, RunStatus, UsageEvent, UsageKind, User


AUTO_TITLE_MAX = 30


def auto_title(content: str) -> str | None:
    """新对话的第一条消息自动成为标题：取第一行、去空白，超长截断并加省略号。全是空白则不设标题。"""
    stripped = content.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0].strip()
    return first_line if len(first_line) <= AUTO_TITLE_MAX else first_line[:AUTO_TITLE_MAX] + "…"


@dataclasses.dataclass(frozen=True)
class Budget:
    """每个用户最近 24 小时的模型费用上限（decisions/0032）。价格是每百万 token 的美元数，与用量看板一致。"""

    daily_usd: float  # 0 表示不限
    price_input: float
    price_cached: float
    price_output: float


class RunService:
    """每个方法一个事务，边界清晰：成功即全部提交，失败即全部回滚。"""

    def __init__(self, sessionmaker: async_sessionmaker, budget: Budget | None = None):
        self.sessionmaker = sessionmaker
        self.budget = budget

    async def _check_budget(self, session, user_id: uuid.UUID) -> None:
        """发起新的回答前检查：最近 24 小时的模型费用到了上限就拒绝（429）。管理员不限。

        用费用而不是 token 数：缓存命中的输入便宜约 50 倍，按 token 数会把正常的多轮对话算得很贵。
        只在“发消息、重试”时检查，不在执行中途打断（例如已经批准的调研计划）；图谱构建有自己的预估和确认（0019），
        它的费用计入用量、会让之后的提问更早到上限，但构建本身不受这里限制。
        """
        b = self.budget
        if b is None or b.daily_usd <= 0:
            return
        if await session.scalar(select(User.role).where(User.id == user_id)) == Role.ADMIN:
            return
        e = UsageEvent
        cost = ((e.input_tokens - e.cached_tokens) * b.price_input + e.cached_tokens * b.price_cached
                + e.output_tokens * b.price_output) / 1_000_000
        used = await session.scalar(select(func.coalesce(func.sum(cost), 0)).where(
            e.user_id == user_id, e.kind == UsageKind.LLM, e.created_at > func.now() - text("interval '24 hours'")))
        if used >= b.daily_usd:
            raise QuotaExceeded(float(used), b.daily_usd)

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
                await self._check_budget(session, user_id)
                if conv.title is None:
                    conv.title = auto_title(content)
                conv.last_activity_at = func.now()  # 与消息同一事务：列表排序与消息内容不会不一致
                msg = await MessageRepository(session).add(conversation_id, MessageRole.USER, content)
                run = await AgentRunRepository(session).create(conversation_id, msg.id)
                # 请求量（decisions/0023）：与 run 同一事务记一行，不会出现“有请求没记录”或反过来
                await session.execute(usage_insert(user_id, UsageKind.RUN, conv.agent, run_id=run.id))
                await notify_jobs(session, "run")  # 提交时才送达：worker 立刻被唤醒（decisions/0020）
                return run
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
        self, run_id: uuid.UUID, conversation_id: uuid.UUID, content: str, citations: list | None = None,
        *, version: int | None = None, attachments: list[dict] | None = None,
    ) -> uuid.UUID | None:
        """助手回复落库 + run 置为 succeeded，同一事务，返回助手消息 id。

        attachments（decisions/0022）：产物元数据在同一事务里写入 artifacts 表，消息里存快照。
        run 不在 running 时整体回滚（不留孤儿消息和产物记录），返回 None。
        """
        async with self.sessionmaker() as session, session.begin():
            snapshot = None
            if attachments:
                conv = await ConversationRepository(session).get(conversation_id)
                for a in attachments:
                    session.add(Artifact(id=uuid.UUID(str(a["id"])), user_id=conv.user_id, conversation_id=conversation_id,
                                         run_id=run_id, filename=a["filename"], content_type=a["content_type"],
                                         size=a["size"], blob_key=a["blob_key"]))
                snapshot = [{k: str(a[k]) if k == "id" else a[k] for k in ("id", "filename", "content_type", "size")}
                            for a in attachments]
            msg = await MessageRepository(session).add(
                conversation_id, MessageRole.ASSISTANT, content, citations=citations, attachments=snapshot
            )
            ok = await AgentRunRepository(session).finish(
                run_id, RunStatus.SUCCEEDED, output_message_id=msg.id, version=version
            )
            if not ok:
                await session.rollback()
                return None
            return msg.id

    async def fail(
        self, run_id: uuid.UUID, error: str, error_code: str = ErrorCode.INTERNAL, *, version: int | None = None
    ) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(
                run_id, RunStatus.FAILED, error=error, error_code=error_code, version=version
            )

    async def cancel_queued(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).cancel_queued(run_id)

    async def cancel_running(self, run_id: uuid.UUID, *, version: int | None = None) -> bool:
        """running -> cancelled。与 complete/fail 一样只允许从 running 转出，先到者赢。"""
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).finish(run_id, RunStatus.CANCELLED, version=version)

    async def await_approval(self, run_id: uuid.UUID, request: dict, *, version: int) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).await_approval(run_id, request, version=version)

    async def respond(self, user_id: uuid.UUID, run_id: uuid.UUID, response: dict) -> AgentRun:
        """用户批准或给出修改意见（decisions/0022）：run 回到 queued，事务内发通知唤醒 worker。"""
        async with self.sessionmaker() as session, session.begin():
            runs = AgentRunRepository(session)
            run = await runs.get(run_id)
            conv = await ConversationRepository(session).get(run.conversation_id) if run else None
            if run is None or conv is None or conv.user_id != user_id:
                raise RunNotFound(str(run_id))
            if not await runs.respond(run_id, response):
                raise RunNotAwaitingApproval(str(run_id))
            await notify_jobs(session, "run")
        return await self.find(run_id)

    async def expire_approvals(self, requested_before) -> list[uuid.UUID]:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).expire_approvals(requested_before)

    async def request_cancel(self, run_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            return await AgentRunRepository(session).request_cancel(run_id)

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
                await self._check_budget(session, user_id)
                messages = await MessageRepository(session).list_by_conversation(run.conversation_id)
                if not messages or messages[-1].id != run.input_message_id:
                    raise RunNotRetryable("only the latest message can be retried")
                new = await runs.create(run.conversation_id, run.input_message_id)
                await session.execute(usage_insert(user_id, UsageKind.RUN, conv.agent, run_id=new.id))
                await notify_jobs(session, "run")
                return new
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
                # 执行时再按可见性过滤一次（decisions/0023）：关联之后知识库可能被取消共享、或用户换了部门
                knowledge_base_ids=tuple(await access.visible_kb_ids(
                    session, conv.user_id, await conversations.knowledge_base_ids(run.conversation_id))),
                agent=conv.agent,
                approval=run.approval_response,
            )

