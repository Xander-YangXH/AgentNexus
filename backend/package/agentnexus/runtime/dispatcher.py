"""进程内派发器 + worker（阶段 1 单机版）。

职责分工：
- 数据库是事实来源：run 以 queued 落库即代表“必须被执行”；
- 内存队列只是加速触发；丢了没关系，周期性扫描会把 queued 的 run 补投；
- 正确性（谁能执行、不重复执行）由 RunService.claim 的原子条件更新保证，而不是靠队列。

单进程假设：启动恢复会把遗留的 running 置为 failed，只有整个系统只有一个这样的进程时才安全。
多实例会互相误杀，所以阶段 4 要换成 lease/heartbeat（见 docs/decisions/0005）。

事件（decisions/0007）：认领后发 run.started；终态事件一律“先提交数据库、再发布”，
所以客户端收到 run.completed 时助手消息一定已经落库。事件发不出去不影响 run 本身。
"""

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import RunHandler
from agentnexus.events import NULL_SINK, RUN_COMPLETED, RUN_FAILED, RUN_STARTED
from agentnexus.events.redis_stream import RunEventPublisher
from agentnexus.services.runs import RunService

log = logging.getLogger(__name__)


class RunDispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        handler: RunHandler,
        *,
        concurrency: int = 4,
        run_timeout: float = 300.0,
        sweep_interval: float = 5.0,
        events: RunEventPublisher | None = None,
    ):
        self._svc = RunService(sessionmaker)
        self._events = events  # None：不发事件（单元测试、或不需要流式的部署）
        self._handler = handler
        self._concurrency = concurrency
        self._run_timeout = run_timeout
        self._sweep_interval = sweep_interval
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        # 已入队或正在处理的 run：避免“提交时入队”和“扫描补投”重复排队（正确性仍由 claim 兜底）
        self._pending: set[uuid.UUID] = set()
        self._tasks: list[asyncio.Task] = []

    # ---------- 对外接口 ----------

    def enqueue(self, run_id: uuid.UUID) -> bool:
        """投递一个 run。不会抛错、不阻塞；返回是否真的入队。"""
        if run_id in self._pending:
            return False
        self._pending.add(run_id)
        self._queue.put_nowait(run_id)
        return True

    async def sweep(self) -> int:
        """把库里 queued 的 run 补投进队列，返回新入队条数。"""
        return sum(self.enqueue(rid) for rid in await self._svc.list_queued_ids())

    async def start(self) -> None:
        error = "interrupted: worker restarted"
        orphaned = await self._svc.fail_orphaned_running(error)
        if orphaned:
            log.warning("startup recovery: marked %d orphaned running run(s) as failed", len(orphaned))
        # 补发终态事件：上一个进程没来得及发，正在订阅的客户端靠它结束（SSE 的查库兜底是第二道保险）
        for run_id in orphaned:
            await self._emit(run_id, RUN_FAILED, {"error": error})
        await self.sweep()
        self._tasks = [asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)]
        self._tasks.append(asyncio.create_task(self._sweep_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------- 内部 ----------

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                n = await self.sweep()
                if n:
                    log.info("sweep re-enqueued %d queued run(s)", n)
            except Exception:
                log.exception("sweep failed")  # 数据库短暂不可用不应让扫描线程退出

    async def _worker_loop(self) -> None:
        while True:
            run_id = await self._queue.get()
            try:
                await self._process(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected error while processing run %s", run_id)
            finally:
                self._pending.discard(run_id)
                self._queue.task_done()

    async def _process(self, run_id: uuid.UUID) -> None:
        if not await self._svc.claim(run_id):
            return  # 已被别人抢走、或已不是 queued（例如已结束），正常情况
        await self._emit(run_id, RUN_STARTED, {})

        ctx = None
        try:
            ctx = await self._svc.load_context(run_id)
            sink = self._events.sink(run_id) if self._events else NULL_SINK
            ctx = dataclasses.replace(ctx, events=sink)
            content = await asyncio.wait_for(self._handler(ctx), timeout=self._run_timeout)
        except asyncio.CancelledError:
            # 关停：run 已是 running，不能留着不管。shield 保证收尾写库不被第二次取消打断
            with contextlib.suppress(Exception):
                await asyncio.shield(self._fail(run_id, "interrupted: shutdown"))
            raise
        except TimeoutError:
            await self._fail(run_id, f"timeout after {self._run_timeout}s")
        except Exception as exc:
            await self._fail(run_id, f"{type(exc).__name__}: {exc}"[:2000])
        else:
            message_id = await self._svc.complete(run_id, ctx.conversation_id, content)
            if message_id is None:
                log.warning("run %s was no longer running when it finished; result discarded", run_id)
            else:
                await self._emit(run_id, RUN_COMPLETED, {"message_id": str(message_id)})

    async def _fail(self, run_id: uuid.UUID, error: str) -> None:
        if await self._svc.fail(run_id, error):  # 先提交
            await self._emit(run_id, RUN_FAILED, {"error": error})  # 后发布

    async def _emit(self, run_id: uuid.UUID, type: str, data: dict[str, Any]) -> None:
        if self._events is not None:
            await self._events.publish(run_id, type, data)
