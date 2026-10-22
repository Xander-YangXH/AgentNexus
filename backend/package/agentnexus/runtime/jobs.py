"""后台任务执行器：文档入库（0016）与图谱构建（0019）共用，阶段 4.1 起基于 PostgreSQL 租约（decisions/0020）。

与 RunDispatcher 同一套思路：
- 数据库是事实来源：任务以 pending 落库即代表“必须被执行”；提交时在同一事务里发 NOTIFY 唤醒，丢了靠轮询兜底；
- 正确性靠租约：认领（SKIP LOCKED）、心跳续约、写回时校验版本号。可以有多个 worker 进程；
- 被打断的任务（崩溃、暂停、关停）由别的 worker 接管重做：两类任务都可以安全重做（入库幂等、本地模型不花钱；
  图谱的抽取结果有缓存，重做不重复花钱）。

比 RunDispatcher 简单：没有取消、流式事件和超时。RunDispatcher 独立保留：取消、超时、事件这些它独有的逻辑占了大半。
"""

import asyncio
import contextlib
import logging
import uuid
from typing import Protocol

from agentnexus.runtime.lease import Claimed, LeaseQueue

log = logging.getLogger(__name__)


class JobService(Protocol):
    queue: LeaseQueue

    async def execute(self, job: Claimed) -> bool: ...


class JobDispatcher:
    def __init__(
        self,
        service: JobService,
        *,
        name: str,
        concurrency: int = 1,
        sweep_interval: float = 5.0,
        heartbeat_seconds: float = 10.0,
        owner: str | None = None,
    ):
        from agentnexus.runtime.dispatcher import default_owner

        self._svc = service
        self._name = name  # 日志里区分是哪类任务
        self._concurrency = concurrency
        self._sweep_interval = sweep_interval
        self._heartbeat_seconds = heartbeat_seconds
        self.owner = owner or default_owner()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def enqueue(self, job_id: uuid.UUID | None = None) -> bool:
        """唤醒空闲的执行槽去认领（真正的入队是数据库里的 pending 记录，提交时已发 NOTIFY）。"""
        self._wake.set()
        return True

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(self._concurrency)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                job = await self._svc.queue.claim_next(self.owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("claiming a %s job failed", self._name)
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._sweep_interval)
                continue
            await self._run(job)

    async def _run(self, job: Claimed) -> None:
        task = asyncio.create_task(self._svc.execute(job))
        heartbeat = asyncio.create_task(self._heartbeat(job, task))
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() == 0:
                return  # 心跳发现租约丢了，取消了执行任务：结果以接管者为准
            # 关停：放弃租约，让别的 worker 立刻接手
            with contextlib.suppress(Exception):
                await asyncio.shield(self._svc.queue.release(job))
            raise
        except Exception:
            # execute 自己会把业务失败记成 failed；走到这里多半是数据库不可用。不收尾：租约过期后由别人接管
            log.exception("unexpected error while processing %s job %s", self._name, job.id)
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, job: Claimed, task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                alive = await self._svc.queue.renew(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("lease renewal failed for %s job %s", self._name, job.id, exc_info=True)
                continue
            if alive is None:
                log.warning("lease lost for %s job %s; stopping", self._name, job.id)
                task.cancel()
                return
