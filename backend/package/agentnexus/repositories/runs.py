import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import ACTIVE_STATUSES, AgentRun, RunStatus


def purge_candidates_stmt(finished_before: datetime, limit: int):
    # WHERE 必须包含部分索引 ix_agent_runs_checkpoint_purge_candidates 的条件
    # （finished_at IS NOT NULL AND checkpoint_purged_at IS NULL），规划器才能使用该索引；测试用 EXPLAIN 验证
    return (
        select(AgentRun.id)
        .where(
            AgentRun.finished_at.is_not(None),
            AgentRun.finished_at < finished_before,
            AgentRun.checkpoint_purged_at.is_(None),
        )
        .order_by(AgentRun.finished_at)
        .limit(limit)
    )


class AgentRunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, conversation_id: uuid.UUID, input_message_id: uuid.UUID) -> AgentRun:
        run = AgentRun(
            conversation_id=conversation_id,
            input_message_id=input_message_id,
            status=RunStatus.QUEUED,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def get_active(self, conversation_id: uuid.UUID) -> AgentRun | None:
        """该对话正在排队或执行的 run（部分唯一索引保证至多一个）。"""
        stmt = select(AgentRun).where(
            AgentRun.conversation_id == conversation_id,
            AgentRun.status.in_(ACTIVE_STATUSES),
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def list_queued(self, limit: int = 100) -> list[AgentRun]:
        """补投递扫描用：队列丢了任务时，靠库里的 queued 记录重新派发。"""
        stmt = (
            select(AgentRun)
            .where(AgentRun.status == RunStatus.QUEUED)
            .order_by(AgentRun.created_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def claim(self, run_id: uuid.UUID) -> bool:
        """queued -> running 的原子抢占。条件更新 + rowcount：只有一个调用者会拿到 True。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.QUEUED)
            .values(status=RunStatus.RUNNING, started_at=func.now())
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def finish(
        self,
        run_id: uuid.UUID,
        status: RunStatus,
        *,
        output_message_id: uuid.UUID | None = None,
        error: str | None = None,
        error_code: str | None = None,
        version: int | None = None,
    ) -> bool:
        """running -> 终态。只允许从 running 转出，防止已结束的 run 被覆盖。

        version：执行者持有的租约版本（fencing token，decisions/0020）。给了就必须匹配——被接管之后，旧 worker 的写回一定失败。
        """
        conds = [AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING]
        if version is not None:
            conds.append(AgentRun.lease_version == version)
        stmt = (
            update(AgentRun)
            .where(*conds)
            .values(
                status=status,
                output_message_id=output_message_id,
                error=error,
                error_code=error_code,
                finished_at=func.now(),
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def cancel_queued(self, run_id: uuid.UUID) -> bool:
        """queued / awaiting_approval -> cancelled 的原子转换（这两种状态下都没有人在执行它）。

        与 claim 用同一个条件（status='queued'），两者并发时只有一个能成功。
        """
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_([RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL]))
            .values(status=RunStatus.CANCELLED, finished_at=func.now())
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def await_approval(self, run_id: uuid.UUID, request: dict, *, version: int) -> bool:
        """running -> awaiting_approval（decisions/0022）：结束这次执行、放弃租约（不占 worker），记下要审批的内容。
        带租约版本校验：被接管的旧执行者不能把 run 改成等待审批。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING, AgentRun.lease_version == version)
            .values(status=RunStatus.AWAITING_APPROVAL, approval_request=request, approval_response=None,
                    approval_requested_at=func.now(), lease_owner=None, lease_expires_at=None)
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def respond(self, run_id: uuid.UUID, response: dict) -> bool:
        """awaiting_approval -> queued，记下用户的答复。attempts 清零：审批是人的动作，不算“被打断的重试”，
        否则多轮修改计划会耗尽接管次数上限（decisions/0020 的 max_attempts）。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.AWAITING_APPROVAL)
            .values(status=RunStatus.QUEUED, approval_response=response, approval_request=None, attempts=0)
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def expire_approvals(self, requested_before: datetime) -> list[uuid.UUID]:
        """等待审批太久的 run 取消掉（之后 checkpoint 由清理任务按 finished_at 回收）。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.status == RunStatus.AWAITING_APPROVAL, AgentRun.approval_requested_at < requested_before)
            .values(status=RunStatus.CANCELLED, error_code="approval_expired", finished_at=func.now())
            .returning(AgentRun.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_by_conversation(self, conversation_id: uuid.UUID) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_checkpoint_purge_candidates(self, finished_before: datetime, limit: int) -> list[uuid.UUID]:
        """已结束超过保留期、checkpoint 还没清理的 run。"""
        return list((await self.session.scalars(purge_candidates_stmt(finished_before, limit))).all())

    async def mark_checkpoint_purged(self, run_ids: list[uuid.UUID]) -> int:
        stmt = (
            update(AgentRun)
            .where(AgentRun.id.in_(run_ids), AgentRun.checkpoint_purged_at.is_(None))
            .values(checkpoint_purged_at=func.now())
        )
        return (await self.session.execute(stmt)).rowcount

    async def request_cancel(self, run_id: uuid.UUID) -> bool:
        """执行中的 run 记下取消请求（跨进程取消的事实来源，decisions/0020）。重复请求不改时间。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING)
            .values(cancel_requested_at=func.coalesce(AgentRun.cancel_requested_at, func.now()))
        )
        return (await self.session.execute(stmt)).rowcount == 1
