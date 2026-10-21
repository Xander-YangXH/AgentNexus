"""进程内派发器 + worker（阶段 1 单机版）。

职责分工：
- 数据库是事实来源：run 以 queued 落库即代表“必须被执行”；
- 内存队列只是加速触发；丢了没关系，周期性扫描会把 queued 的 run 补投；
- 正确性（谁能执行、不重复执行）由 RunService.claim 的原子条件更新保证，而不是靠队列。

单进程假设：启动恢复会把遗留的 running 置为 failed，只有整个系统只有一个这样的进程时才安全。
多实例会互相误杀，所以阶段 4 要换成 lease/heartbeat（见 docs/decisions/0005）。

事件（decisions/0007）：认领后发 run.started；终态事件一律“先提交数据库、再发布”，
所以客户端收到 run.completed 时助手消息一定已经落库。事件发不出去不影响 run 本身。

取消（decisions/0009）：每个 run 的 Agent 在独立的任务里执行，取消只取消这个任务，不影响 worker 循环。
“用户取消”和“服务关停”在代码里都是 CancelledError，用 Task.cancelling() 区分：
外层 worker 任务本身正在被取消 = 关停；否则 = 用户取消。
单进程限制：取消信号只在本进程内传递，阶段 4 拆出独立 worker 后要改成跨进程信号（Redis 或数据库标记）。
"""

import asyncio
import contextlib
import dataclasses
import logging
import enum
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import RunHandler
from agentnexus.events import NULL_SINK, RUN_CANCELLED, RUN_COMPLETED, RUN_FAILED, RUN_STARTED
from agentnexus.events.redis_stream import RunEventPublisher
from agentnexus.observability.context import run_id_var
from agentnexus.runtime.failures import ErrorCode, classify
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus

log = logging.getLogger(__name__)


class CancelOutcome(enum.StrEnum):
    CANCELLED = "cancelled"  # 还在排队，已直接取消
    REQUESTED = "requested"  # 正在执行，已发出取消信号；最终结果以 run.cancelled 事件 / 数据库状态为准
    FINISHED = "finished"  # 已经结束（成功、失败或早已取消），无需取消


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
        # 正在执行的 run -> 执行它的任务；以及“已请求取消”的 run（覆盖“已认领、任务还没建”的空档）
        self._inflight: dict[uuid.UUID, asyncio.Task] = {}
        self._cancel_requested: set[uuid.UUID] = set()

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
        orphaned = await self._svc.fail_orphaned_running("interrupted: worker restarted", ErrorCode.INTERRUPTED)
        if orphaned:
            log.warning("startup recovery: marked %d orphaned running run(s) as failed", len(orphaned))
        # 补发终态事件：上一个进程没来得及发，正在订阅的客户端靠它结束（SSE 的查库兜底是第二道保险）
        for run_id in orphaned:
            await self._emit(run_id, RUN_FAILED, {"error_code": ErrorCode.INTERRUPTED})
        await self.sweep()
        self._tasks = [asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)]
        self._tasks.append(asyncio.create_task(self._sweep_loop()))

    async def cancel(self, run_id: uuid.UUID) -> CancelOutcome:
        """请求取消一个 run。调用方负责归属校验。"""
        # 1) 还在排队：直接在数据库里 queued -> cancelled。与 claim 用同一个条件，二者并发只有一个成功
        if await self._svc.cancel_queued(run_id):
            await self._emit(run_id, RUN_CANCELLED, {})
            return CancelOutcome.CANCELLED
        # 2) 没取消成功：要么刚被认领（running），要么已经结束
        run = await self._svc.find(run_id)
        if run is None or run.status != RunStatus.RUNNING:
            return CancelOutcome.FINISHED
        # 3) 正在执行：先登记，再取消任务。登记是为了覆盖“已认领、任务还没创建”的空档（_process 会检查）
        self._cancel_requested.add(run_id)
        task = self._inflight.get(run_id)
        if task is not None:
            task.cancel()
        return CancelOutcome.REQUESTED

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
        # 这段时间内的所有日志（包括 Agent 任务里的：create_task 会复制当前上下文）都自动带上 run_id
        token = run_id_var.set(str(run_id))
        try:
            await self._process_claimed(run_id)
        finally:
            run_id_var.reset(token)

    async def _process_claimed(self, run_id: uuid.UUID) -> None:
        if not await self._svc.claim(run_id):
            self._cancel_requested.discard(run_id)
            return  # 已被别人抢走、或已不是 queued（例如已结束、已取消），正常情况
        await self._emit(run_id, RUN_STARTED, {})
        log.info("run started")  # run_id 由上下文自动带上
        started = time.perf_counter()
        outcome = "succeeded"  # 结束时记一条带结局与耗时的日志，以后做 Dashboard 可直接统计

        try:
            ctx = await self._svc.load_context(run_id)
            sink = self._events.sink(run_id) if self._events else NULL_SINK
            ctx = dataclasses.replace(ctx, events=sink)
            task = asyncio.create_task(self._handler(ctx))
            self._inflight[run_id] = task
            if run_id in self._cancel_requested:  # 取消请求在任务创建之前就到了
                task.cancel()
            content = await asyncio.wait_for(task, timeout=self._run_timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if run_id in self._cancel_requested and current is not None and current.cancelling() == 0:
                # 用户取消：外层 worker 任务没有被取消，被取消的只是执行 Agent 的那个任务。收尾后不再向上抛
                outcome = "cancelled"
                if await self._svc.cancel_running(run_id):
                    await self._emit(run_id, RUN_CANCELLED, {})
                return
            # 关停：run 已是 running，不能留着不管。shield 保证收尾写库不被第二次取消打断
            outcome = ErrorCode.INTERRUPTED
            with contextlib.suppress(Exception):
                await asyncio.shield(self._fail(run_id, "interrupted: shutdown", ErrorCode.INTERRUPTED))
            raise
        except TimeoutError:
            outcome = ErrorCode.TIMEOUT
            await self._fail(run_id, f"timeout after {self._run_timeout}s", ErrorCode.TIMEOUT)
        except Exception as exc:
            outcome = classify(exc)
            log.warning("run failed", exc_info=True)  # 原始异常写日志，便于排查（run_id 由上下文带上）
            await self._fail(run_id, f"{type(exc).__name__}: {exc}"[:2000], outcome)
        else:
            message_id = await self._svc.complete(run_id, ctx.conversation_id, content)
            if message_id is None:
                outcome = "discarded"
                log.warning("run was no longer running when it finished; result discarded")
            else:
                await self._emit(run_id, RUN_COMPLETED, {"message_id": str(message_id)})
        finally:
            self._inflight.pop(run_id, None)
            self._cancel_requested.discard(run_id)
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.info("run finished: %s", outcome, extra={"outcome": str(outcome), "duration_ms": duration_ms})

    async def _fail(self, run_id: uuid.UUID, error: str, error_code: ErrorCode) -> None:
        # 原始报错只进数据库（error 列）；事件里只放错误码
        if await self._svc.fail(run_id, error, error_code):  # 先提交
            await self._emit(run_id, RUN_FAILED, {"error_code": error_code})  # 后发布

    async def _emit(self, run_id: uuid.UUID, type: str, data: dict[str, Any]) -> None:
        if self._events is not None:
            await self._events.publish(run_id, type, data)
