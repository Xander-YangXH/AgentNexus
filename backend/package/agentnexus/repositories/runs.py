import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import AgentRun, RunStatus


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
            AgentRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
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
    ) -> bool:
        """running -> 终态。只允许从 running 转出，防止已结束的 run 被覆盖。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING)
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
        """queued -> cancelled 的原子转换。与 claim 用同一个条件（status='queued'），两者并发时只有一个能成功。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.QUEUED)
            .values(status=RunStatus.CANCELLED, finished_at=func.now())
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def list_by_conversation(self, conversation_id: uuid.UUID) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def fail_all_running(self, error: str, error_code: str) -> list[uuid.UUID]:
        """把所有 running 的 run 置为 failed，返回它们的 id。仅用于单进程部署的启动恢复。"""
        stmt = (
            update(AgentRun)
            .where(AgentRun.status == RunStatus.RUNNING)
            .values(status=RunStatus.FAILED, error=error, error_code=error_code, finished_at=func.now())
            .returning(AgentRun.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())
