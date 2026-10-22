"""后台任务派发器：文档入库（阶段 2.2，decisions/0016）与图谱构建（阶段 3，decisions/0019）共用。

与 RunDispatcher 同一套思路：数据库是事实来源（任务以 pending 落库即代表“必须被执行”），
内存队列只是加速触发，周期性扫描把 pending 的补投；不重复执行由各服务 process() 里的原子抢占保证。

比 RunDispatcher 简单：没有取消、没有流式事件、没有超时。
第一版叫 JobDispatcher、只服务文档入库；图谱构建是第二个使用者，需要的正好是同一组操作，
于是抽成按协议（JobService）工作的通用类——有了第二个真实用例再抽象，而不是提前猜形状。
RunDispatcher 仍然独立：取消、超时、事件这些它独有的逻辑占了大半。阶段 4 一起换成独立 worker（decisions/0005）。
"""

import asyncio
import logging
import uuid
from typing import Protocol

log = logging.getLogger(__name__)


class JobService(Protocol):
    async def list_pending_ids(self) -> list[uuid.UUID]: ...

    async def requeue_interrupted(self) -> list[uuid.UUID]:
        """启动恢复：上一个进程执行到一半的任务改回 pending（要求任务可以安全地重做）。"""
        ...

    async def process(self, job_id: uuid.UUID) -> bool: ...


class JobDispatcher:
    def __init__(self, service: JobService, *, name: str, concurrency: int = 1, sweep_interval: float = 5.0):
        self._svc = service
        self._name = name  # 日志里区分是哪类任务
        self._concurrency = concurrency
        self._sweep_interval = sweep_interval
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._pending: set[uuid.UUID] = set()  # 已入队或正在处理：避免上传入队与扫描补投重复排队
        self._tasks: list[asyncio.Task] = []

    def enqueue(self, job_id: uuid.UUID) -> bool:
        if job_id in self._pending:
            return False
        self._pending.add(job_id)
        self._queue.put_nowait(job_id)
        return True

    async def sweep(self) -> int:
        return sum(self.enqueue(d) for d in await self._svc.list_pending_ids())

    async def start(self) -> None:
        requeued = await self._svc.requeue_interrupted()
        if requeued:
            log.warning("startup recovery: re-queued %d interrupted %s job(s)", len(requeued), self._name)
        await self.sweep()
        self._tasks = [asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)]
        self._tasks.append(asyncio.create_task(self._sweep_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        # 正在处理的文档停在 processing，下次启动时改回 pending 重做（处理是幂等的）
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                n = await self.sweep()
                if n:
                    log.info("sweep re-enqueued %d pending %s job(s)", n, self._name)
            except Exception:
                log.exception("%s sweep failed", self._name)

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._svc.process(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # process 自己会把业务失败记成 failed；走到这里多半是数据库不可用，
                # 任务留在执行中状态，下次启动时恢复
                log.exception("unexpected error while processing %s job %s", self._name, job_id)
            finally:
                self._pending.discard(job_id)
                self._queue.task_done()
