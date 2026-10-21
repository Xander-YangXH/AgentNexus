"""文档处理的后台派发器（阶段 2.2，decisions/0016）。

与 RunDispatcher 同一套思路：数据库是事实来源（文档以 pending 落库即代表“必须被处理”），
内存队列只是加速触发，周期性扫描把 pending 的补投；不重复处理由 KnowledgeService.process 里的原子抢占保证。

比 RunDispatcher 简单：文档处理没有取消、没有流式事件、没有超时（大文件向量化本来就慢，且不花钱）。
两者没有抽成一个通用类：重复的只有队列与扫描的二三十行，阶段 4 会一起换成独立 worker（decisions/0005），
现在抽象反而要先猜以后的形状。
"""

import asyncio
import logging
import uuid

from agentnexus.services.knowledge import KnowledgeService

log = logging.getLogger(__name__)


class IngestDispatcher:
    def __init__(self, service: KnowledgeService, *, concurrency: int = 1, sweep_interval: float = 5.0):
        self._svc = service
        self._concurrency = concurrency
        self._sweep_interval = sweep_interval
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._pending: set[uuid.UUID] = set()  # 已入队或正在处理：避免上传入队与扫描补投重复排队
        self._tasks: list[asyncio.Task] = []

    def enqueue(self, document_id: uuid.UUID) -> bool:
        if document_id in self._pending:
            return False
        self._pending.add(document_id)
        self._queue.put_nowait(document_id)
        return True

    async def sweep(self) -> int:
        return sum(self.enqueue(d) for d in await self._svc.list_pending_ids())

    async def start(self) -> None:
        requeued = await self._svc.requeue_interrupted()
        if requeued:
            log.warning("startup recovery: re-queued %d interrupted document(s)", len(requeued))
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
                    log.info("sweep re-enqueued %d pending document(s)", n)
            except Exception:
                log.exception("document sweep failed")

    async def _worker_loop(self) -> None:
        while True:
            document_id = await self._queue.get()
            try:
                await self._svc.process(document_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # process 自己会把解析、向量化、索引的失败记成 failed；走到这里多半是数据库不可用，
                # 文档留在 processing，下次启动时恢复
                log.exception("unexpected error while processing document %s", document_id)
            finally:
                self._pending.discard(document_id)
                self._queue.task_done()
