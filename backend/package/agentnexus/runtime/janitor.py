"""清理已结束 run 的 LangGraph checkpoint（decisions/0010）。

为什么要清理：按决策 0006，每次执行都把整段对话历史喂给图，图的状态（含整段历史）存进这个 run 的 checkpoint。
实测（随机中文、每轮约 1KB）：第 20 轮单个 run 的 checkpoint 约 66KB，20 轮累计 756KB，是对话文字本身的 18 倍，
总量随轮数平方增长。checkpoint 只对“进行中”的 run 有用（中断恢复、崩溃接管），run 结束后保留一段时间供排查即可。

怎么记账：用 agent_runs.checkpoint_purged_at 记录“已清理”，不去查询 LangGraph 自己管理的表；
删除用 LangGraph 提供的 adelete_thread（幂等）。先删、后记账：记账失败只会导致下一轮重复删除，而重复删除无害。

顺带（decisions/0022）：等待审批超过 approval_ttl 的 run 取消掉（error_code=approval_expired），
之后它的 checkpoint 按 finished_at 照常进入保留期、再被清理。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.runs import AgentRunRepository

log = logging.getLogger(__name__)


class CheckpointJanitor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpointer: BaseCheckpointSaver,
        *,
        retention: timedelta,
        interval_seconds: float = 600.0,
        batch_size: int = 200,
        approval_ttl: timedelta | None = None,
    ):
        self._sessionmaker = sessionmaker
        self._checkpointer = checkpointer
        self._retention = retention
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._approval_ttl = approval_ttl
        self._task: asyncio.Task | None = None

    async def run_once(self, now: datetime | None = None) -> int:
        """清理一批，返回成功清理的 run 数。单个线程删除失败不影响其他线程，下一轮会重试。"""
        now = now or datetime.now(UTC)
        if self._approval_ttl is not None:
            async with self._sessionmaker() as session, session.begin():
                expired = await AgentRunRepository(session).expire_approvals(now - self._approval_ttl)
            if expired:
                log.info("cancelled %d run(s) whose approval expired", len(expired))
        cutoff = now - self._retention
        async with self._sessionmaker() as session:
            candidates = await AgentRunRepository(session).list_checkpoint_purge_candidates(cutoff, self._batch_size)
        purged = []
        for run_id in candidates:
            try:
                await self._checkpointer.adelete_thread(str(run_id))  # 决策 0006：thread_id 就是 run_id
                purged.append(run_id)
            except Exception:
                log.warning("deleting checkpoint of run %s failed; will retry next round", run_id, exc_info=True)
        if purged:
            async with self._sessionmaker() as session, session.begin():
                await AgentRunRepository(session).mark_checkpoint_purged(purged)
            log.info("purged checkpoints of %d finished run(s)", len(purged))
        return len(purged)

    async def _loop(self) -> None:
        while True:
            try:
                # 一次可能有很多积压：一批满了就马上处理下一批，处理不满再休息
                while await self.run_once() >= self._batch_size:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("checkpoint janitor round failed")  # 数据库短暂不可用不应让清理协程退出
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
