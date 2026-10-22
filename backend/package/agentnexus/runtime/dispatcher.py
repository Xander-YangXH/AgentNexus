"""run 的派发与执行（阶段 4.1 起基于 PostgreSQL 租约，decisions/0020；之前的进程内版本见 0005）。

职责分工：
- 数据库是事实来源：run 以 queued 落库即代表“必须被执行”；提交时在同一事务里发 NOTIFY，worker 立刻被唤醒，
  通知丢了也没关系，每个执行槽空闲时按 sweep_interval 轮询（同时用来发现租约过期的 run）；
- 正确性靠租约：认领（SKIP LOCKED）、心跳续约、写回时校验版本号（fencing token）。所以可以有多个 worker 进程，
  也不再需要“启动时把遗留的 running 标为失败”（那条恢复规则只在单进程下成立）；
- 被打断的 run（worker 崩溃、被暂停、优雅关停）由别的 worker 接管，并从 LangGraph checkpoint 续做（0020），最多 max_attempts 次。

事件（decisions/0007）：认领后发 run.started（第一次）或 run.resumed（接管后）；终态事件一律“先提交数据库、再发布”。
取消（decisions/0009、0020）：取消请求写进数据库（事实来源）；在本进程执行的直接取消任务，其他进程通过 Redis 广播（cancel_bus）
收到，心跳时也会检查数据库标记兜底。“用户取消”和“服务关停”都是 CancelledError，用 Task.cancelling() 区分。
"""

import asyncio
import contextlib
import dataclasses
import enum
import logging
import os
import socket
import time
import uuid
from typing import Any, Protocol

from sqlalchemy import func
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.agents.types import AgentReply, RunHandler
from agentnexus.events import NULL_SINK, RUN_CANCELLED, RUN_COMPLETED, RUN_FAILED, RUN_RESUMED, RUN_STARTED
from agentnexus.events.redis_stream import RunEventPublisher
from agentnexus.observability.context import run_id_var
from agentnexus.runtime.failures import ErrorCode, classify
from agentnexus.runtime.lease import Claimed, LeaseQueue
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import AgentRun, RunStatus

log = logging.getLogger(__name__)


class CancelOutcome(enum.StrEnum):
    CANCELLED = "cancelled"  # 还在排队，已直接取消
    REQUESTED = "requested"  # 正在执行，已发出取消请求；最终结果以 run.cancelled 事件 / 数据库状态为准
    FINISHED = "finished"  # 已经结束（成功、失败或早已取消），无需取消


class CancelBus(Protocol):
    """跨进程的取消广播（runtime/cancel_bus.py 用 Redis 实现）。只用来加速：丢了也没关系，心跳会检查数据库标记。"""

    async def publish(self, run_id: uuid.UUID) -> None: ...


def default_owner() -> str:
    """租约持有者标识：主机名 + 进程号 + 随机后缀（同一容器重启后进程号可能相同）。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"[:64]


class _AttemptSink:
    """接管后的尝试：token 事件带上 attempt。前端收到 run.resumed 后忽略更早尝试的 token——
    旧 worker 在心跳发现自己被接管之前（最多一个心跳间隔）流出的 token 不会混进新的回复。"""

    def __init__(self, sink, attempt: int):
        self._sink, self._attempt = sink, attempt

    async def emit(self, type: str, data: dict[str, Any]) -> None:
        await self._sink.emit(type, {**data, "attempt": self._attempt})


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
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 10.0,
        max_attempts: int = 3,
        owner: str | None = None,
        cancel_bus: CancelBus | None = None,
    ):
        self._svc = RunService(sessionmaker)
        self._queue = LeaseQueue(
            sessionmaker, AgentRun, pending=RunStatus.QUEUED, running=RunStatus.RUNNING,
            lease_seconds=lease_seconds, on_claim={"started_at": func.now()}, notify_kind="run",
        )
        self._events = events  # None：不发事件（单元测试、或不需要流式的部署）
        self._handler = handler
        self._concurrency = concurrency
        self._run_timeout = run_timeout
        self._sweep_interval = sweep_interval
        self._heartbeat_seconds = heartbeat_seconds
        self._max_attempts = max_attempts
        self.owner = owner or default_owner()
        self._cancel_bus = cancel_bus
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # 本进程正在执行的 run -> 执行它的任务；“已请求取消”的 run（覆盖“已认领、任务还没建”的空档）；租约已丢失的 run
        self._inflight: dict[uuid.UUID, asyncio.Task] = {}
        self._cancel_requested: set[uuid.UUID] = set()
        self._lease_lost: set[uuid.UUID] = set()

    # ---------- 对外接口 ----------

    def enqueue(self, run_id: uuid.UUID | None = None) -> bool:
        """唤醒空闲的执行槽去认领。真正的“入队”是数据库里的 queued 记录（提交时已在事务里发了 NOTIFY）。"""
        self._wake.set()
        return True

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)]

    async def cancel(self, run_id: uuid.UUID) -> CancelOutcome:
        """请求取消一个 run。调用方负责归属校验。api 进程和 worker 进程都可以调用。"""
        # 1) 还在排队：直接在数据库里 queued -> cancelled（与认领互斥：认领要求 status 为 queued 或租约过期的 running）
        if await self._svc.cancel_queued(run_id):
            await self._emit(run_id, RUN_CANCELLED, {})
            return CancelOutcome.CANCELLED
        # 2) 正在执行：记下取消请求（事实来源），本进程在执行就直接取消，否则广播给执行它的进程
        if not await self._svc.request_cancel(run_id):
            return CancelOutcome.FINISHED
        self._cancel_local(run_id)
        if self._cancel_bus is not None and run_id not in self._inflight:
            await self._cancel_bus.publish(run_id)
        return CancelOutcome.REQUESTED

    def on_cancel_message(self, run_id: uuid.UUID) -> None:
        """收到别的进程广播的取消请求：只有在本进程执行的 run 才处理。"""
        if run_id in self._inflight:
            self._cancel_local(run_id)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------- 内部 ----------

    def _cancel_local(self, run_id: uuid.UUID) -> None:
        self._cancel_requested.add(run_id)
        task = self._inflight.get(run_id)
        if task is not None:
            task.cancel()

    async def _worker_loop(self) -> None:
        while True:
            self._wake.clear()  # 先清再抢：清之后到来的通知不会丢（抢的时候一定能看到对应的记录）
            try:
                job = await self._queue.claim_next(self.owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("claiming a run failed")  # 数据库短暂不可用：等一会儿再试，不让执行槽退出
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._sweep_interval)
                continue
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected error while processing run %s", job.id)

    async def _process(self, job: Claimed) -> None:
        # 这段时间内的所有日志（包括 Agent 任务里的：create_task 会复制当前上下文）都自动带上 run_id
        token = run_id_var.set(str(job.id))
        try:
            await self._process_claimed(job)
        finally:
            run_id_var.reset(token)
            self._inflight.pop(job.id, None)
            self._cancel_requested.discard(job.id)
            self._lease_lost.discard(job.id)

    async def _process_claimed(self, job: Claimed) -> None:
        run_id = job.id
        if job.attempts > self._max_attempts:
            # 反复被打断（例如每次执行都会让 worker 崩溃）：不再尝试，避免无限循环、无限花钱
            log.warning("run exceeded %d attempts; giving up", self._max_attempts)
            await self._fail(job, f"interrupted: gave up after {self._max_attempts} attempts", ErrorCode.INTERRUPTED)
            return
        run = await self._svc.find(run_id)
        if run.cancel_requested_at is not None:
            # 用户已经请求取消，但上一个执行者没来得及处理就被打断了（例如取消后紧接着关停）：直接收尾为已取消，不再执行
            if await self._svc.cancel_running(run_id, version=job.version):
                await self._emit(run_id, RUN_CANCELLED, {})
            log.info("run cancelled before (re)execution")
            return
        resumed = job.attempts > 1
        await self._emit(run_id, RUN_RESUMED if resumed else RUN_STARTED, {"attempt": job.attempts} if resumed else {})
        log.info("run resumed" if resumed else "run started", extra={"attempt": job.attempts})
        started = time.perf_counter()
        outcome = "succeeded"  # 结束时记一条带结局与耗时的日志，以后做 Dashboard 可直接统计
        heartbeat = None

        try:
            ctx = await self._svc.load_context(run_id)
            sink = self._events.sink(run_id) if self._events else NULL_SINK
            ctx = dataclasses.replace(ctx, events=_AttemptSink(sink, job.attempts) if resumed else sink, attempt=job.attempts)
            task = asyncio.create_task(self._handler(ctx))
            self._inflight[run_id] = task
            heartbeat = asyncio.create_task(self._heartbeat(job, task))
            if run_id in self._cancel_requested:  # 取消请求在任务创建之前就到了
                task.cancel()
            result = await asyncio.wait_for(task, timeout=self._run_timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() == 0:
                if run_id in self._lease_lost:
                    # 租约已被别的 worker 接管（本进程曾被卡住）：什么都不写，结果以新的执行者为准
                    outcome = "lease_lost"
                    return
                if run_id in self._cancel_requested:
                    # 用户取消：外层 worker 任务没有被取消，被取消的只是执行 Agent 的那个任务。收尾后不再向上抛
                    outcome = "cancelled"
                    if await self._svc.cancel_running(run_id, version=job.version):
                        await self._emit(run_id, RUN_CANCELLED, {})
                    return
            # 关停：放弃租约，让别的 worker 立刻接手并从 checkpoint 续做（不再像单进程时那样标为失败）。
            # shield：收尾写库不被第二次取消打断
            outcome = "released"
            with contextlib.suppress(Exception):
                await asyncio.shield(self._queue.release(job))
            raise
        except TimeoutError:
            outcome = ErrorCode.TIMEOUT
            await self._fail(job, f"timeout after {self._run_timeout}s", ErrorCode.TIMEOUT)
        except Exception as exc:
            outcome = classify(exc)
            log.warning("run failed", exc_info=True)  # 原始异常写日志，便于排查（run_id 由上下文带上）
            await self._fail(job, f"{type(exc).__name__}: {exc}"[:2000], outcome)
        else:
            reply = result if isinstance(result, AgentReply) else AgentReply(content=result)
            message_id = await self._svc.complete(
                run_id, ctx.conversation_id, reply.content, reply.citations, version=job.version
            )
            if message_id is None:
                outcome = "discarded"
                log.warning("run was no longer ours when it finished (cancelled or taken over); result discarded")
            else:
                await self._emit(run_id, RUN_COMPLETED, {"message_id": str(message_id)})
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.info("run finished: %s", outcome, extra={"outcome": str(outcome), "duration_ms": duration_ms,
                                                          "attempt": job.attempts})

    async def _heartbeat(self, job: Claimed, task: asyncio.Task) -> None:
        """定期续约。续约失败 = 已被接管：停止执行；数据库里有取消请求：按用户取消处理（跨进程取消的兜底）。"""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                row = await self._queue.renew(job, AgentRun.cancel_requested_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库短暂不可用：继续执行、下次再续。真断开超过租约时长，别人会接管，fencing 保证这边写不进去
                log.warning("lease renewal failed for run %s", job.id, exc_info=True)
                continue
            if row is None:
                log.warning("lease lost for run %s; stopping", job.id)
                self._lease_lost.add(job.id)
                task.cancel()
                return
            if row[0] is not None and job.id not in self._cancel_requested:
                self._cancel_local(job.id)

    async def _fail(self, job: Claimed, error: str, error_code: ErrorCode) -> None:
        # 原始报错只进数据库（error 列）；事件里只放错误码
        if await self._svc.fail(job.id, error, error_code, version=job.version):  # 先提交
            await self._emit(job.id, RUN_FAILED, {"error_code": error_code})  # 后发布

    async def _emit(self, run_id: uuid.UUID, type: str, data: dict[str, Any]) -> None:
        if self._events is not None:
            await self._events.publish(run_id, type, data)
