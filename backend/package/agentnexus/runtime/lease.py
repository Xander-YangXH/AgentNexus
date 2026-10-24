"""PostgreSQL 租约队列（阶段 4.1，decisions/0020）：业务表本身就是任务表。

- 认领：`UPDATE … WHERE id = (SELECT … WHERE 待执行 OR (执行中 AND 租约已过期) … FOR UPDATE SKIP LOCKED LIMIT 1)`。
  多个 worker 同时抢，不会抢到同一行，也不会互相等锁；认领时 lease_version 加一（fencing token）、attempts 加一。
- 续约：心跳时 `UPDATE … SET lease_expires_at = now() + 租约 WHERE id = … AND lease_version = 我的版本`。
  0 行 = 已被别人接管，调用方必须立刻停止执行。
- 写回：业务上的写入（完成、失败、写分块……）都带上 `lease_version = 我的版本` 条件，被接管后旧 worker 的写入一定失败。
- 放弃：优雅关停时把租约改为“立即过期”，别的 worker 马上就能接手，而不是等租约自然过期。

实验数据（backend/eval/queue_exp）：kill -9 后 6 秒接手、0 重复；暂停后恢复的旧 worker 写入被拒。
时间一律用数据库的 now()，不用 worker 本机的时钟：多台机器的时钟不一致时，租约判断仍然一致。
"""

import dataclasses
import uuid
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOTIFY_CHANNEL = "agentnexus_jobs"  # LISTEN/NOTIFY 通道，消息内容是任务类型（run / ingest / graph）


@dataclasses.dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    version: int  # fencing token
    attempts: int  # 含这一次


class LeaseQueue:
    """按表参数化的租约队列。model 需要有 id、status、created_at 和 LeaseMixin 的字段。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        model: Any,
        *,
        pending: str,
        running: str,
        lease_seconds: float = 30.0,
        on_claim: dict | None = None,
        notify_kind: str | None = None,
        not_before: Any = None,
    ):
        self.sessionmaker = sessionmaker
        self.model = model
        self.pending = pending
        self.running = running
        self.lease_seconds = lease_seconds
        self.on_claim = on_claim or {}  # 认领时顺带设置的字段，例如 started_at
        self.notify_kind = notify_kind  # 放弃租约时发的通知类型（run / ingest / graph）
        self.not_before = not_before  # 可选的“最早执行时间”列：失败后退避重试的任务，到时间之前不认领

    def _lease_until(self):
        return func.now() + text(f"interval '{float(self.lease_seconds)} seconds'")

    def _claimable(self):
        m = self.model
        # 租约为空也按过期处理：升级前就在执行中的旧记录（没有租约字段）会被接管
        expired = or_(m.lease_expires_at.is_(None), m.lease_expires_at < func.now())
        pending = m.status == self.pending
        if self.not_before is not None:
            pending = pending & or_(self.not_before.is_(None), self.not_before <= func.now())
        return or_(pending, (m.status == self.running) & expired)

    async def claim_next(self, owner: str) -> Claimed | None:
        m = self.model
        pick = (select(m.id).where(self._claimable()).order_by(m.created_at).limit(1)
                .with_for_update(skip_locked=True).scalar_subquery())
        return await self._claim(m.id == pick, owner)

    async def claim(self, job_id: uuid.UUID, owner: str) -> Claimed | None:
        """认领指定的任务（同步执行时用，例如测试里直接调用 process）。条件与 claim_next 相同。"""
        return await self._claim((self.model.id == job_id) & self._claimable(), owner)

    async def _claim(self, where, owner: str) -> Claimed | None:
        m = self.model
        values = {"status": self.running, "lease_owner": owner, "lease_expires_at": self._lease_until(),
                  "lease_version": m.lease_version + 1, "attempts": m.attempts + 1}
        for col, val in self.on_claim.items():  # 只在第一次认领时设置（如 started_at），接管时保留原值
            values[col] = func.coalesce(getattr(m, col), val)
        stmt = update(m).where(where).values(**values).returning(m.id, m.lease_version, m.attempts)
        async with self.sessionmaker() as session, session.begin():
            row = (await session.execute(stmt)).first()
        return Claimed(*row) if row else None

    async def renew(self, job: Claimed, *extra_columns) -> tuple | None:
        """续约。返回 extra_columns 的当前值（例如 cancel_requested_at）；None 表示租约已丢失（被接管或已结束）。"""
        m = self.model
        stmt = (update(m).where(m.id == job.id, m.lease_version == job.version, m.status == self.running)
                .values(lease_expires_at=self._lease_until()).returning(m.id, *extra_columns))
        async with self.sessionmaker() as session, session.begin():
            row = (await session.execute(stmt)).first()
        return tuple(row[1:]) if row else None

    async def release(self, job: Claimed) -> bool:
        """优雅关停：让租约立即过期，别的 worker 马上可以接手（attempts 不回退：被打断也算一次尝试）。"""
        m = self.model
        stmt = (update(m).where(m.id == job.id, m.lease_version == job.version, m.status == self.running)
                .values(lease_expires_at=func.now() - text("interval '1 second'")))
        async with self.sessionmaker() as session, session.begin():
            released = (await session.execute(stmt)).rowcount == 1
            if released and self.notify_kind:
                # 通知别的 worker 立刻来接：第一版没发，空闲的副本要等下一轮轮询（实测 docker stop 后 5.2 秒才接管）
                await notify_jobs(session, self.notify_kind)
            return released

    def fenced(self, job: Claimed):
        """写回条件：仍在执行中，且租约还是我的。业务写入都要加上它。"""
        return (self.model.id == job.id) & (self.model.status == self.running) & (self.model.lease_version == job.version)


async def notify_jobs(session: AsyncSession, kind: str) -> None:
    """在提交任务的事务里发通知：事务提交时才会送达，回滚就不会送达——不会出现“通知到了、任务却不存在”。"""
    await session.execute(text("SELECT pg_notify(:channel, :kind)"), {"channel": NOTIFY_CHANNEL, "kind": kind})
