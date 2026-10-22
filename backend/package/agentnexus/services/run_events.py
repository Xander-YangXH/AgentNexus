"""SSE 订阅一个 run 的事件流（decisions/0007）。

核心原则：事件只是视图，数据库决定流什么时候结束。
- 正常情况：从 Redis 读到终态事件就结束；
- 事件丢了（流过期、Redis 抖动、上一个进程来不及发）：空闲时查数据库，run 已到终态就按数据库“合成”一个终态事件再结束；
- Redis 不可用：退化成定期查数据库，仍然能在 run 结束时收尾。
- 等待审批（decisions/0022）：流在 run.awaiting_approval 处“暂停”——结束响应，不让客户端挂着连接等几个小时；
  批准后客户端带 Last-Event-ID 重新订阅。流里已经被批准过的旧 run.awaiting_approval（例如刷新后从头回放）
  以数据库为准：run 已不在等待审批，就跳过它继续读，不能让它把流提前结束。
调用方必须已经做过归属校验。
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.events import PAUSE_EVENTS, RUN_AWAITING_APPROVAL, RUN_CANCELLED, RUN_COMPLETED, RUN_FAILED, RunEvent
from agentnexus.events.redis_stream import RunEventReader
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import AgentRun, RunStatus

log = logging.getLogger(__name__)

_FINISHED = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


def _synthesize(run: AgentRun) -> RunEvent:
    """按数据库里的终态构造终态事件。没有 id：它不在流里，不能作为续传游标。"""
    if run.status == RunStatus.SUCCEEDED:
        return RunEvent(RUN_COMPLETED, {"message_id": str(run.output_message_id), "synthesized": True})
    if run.status == RunStatus.CANCELLED:
        return RunEvent(RUN_CANCELLED, {"synthesized": True})
    return RunEvent(RUN_FAILED, {"error_code": run.error_code or "internal", "synthesized": True})


class RunEventStreamer:
    def __init__(
        self, sessionmaker: async_sessionmaker, reader: RunEventReader, *, poll_interval: float = 5.0
    ):
        self._runs = RunService(sessionmaker)
        self._reader = reader
        self._poll_interval = poll_interval

    async def _read(self, run_id: uuid.UUID, cursor: str, block: bool, state: dict) -> list[RunEvent] | None:
        """读事件。返回 [] 表示这段时间没有新事件；返回 None 表示 Redis 不可用（调用方退化为查库）。"""
        try:
            return await self._reader.read(
                run_id, cursor, int(self._poll_interval * 1000) if block else None
            )
        except asyncio.CancelledError:
            # 客户端断开：必须向上抛，交给框架结束这个流。
            # （在 FastAPI 的 SSE 里，anyio 的取消是“电平触发”的，吞掉一次下个 await 还会再被取消；
            #   但在纯 asyncio 任务里吞掉就会一直读下去、占着连接——不能依赖框架兜底。）
            raise
        except RedisTimeoutError:
            # 客户端套接字先于 Redis 的 BLOCK 超时：只说明这段时间没有数据，按空闲处理。
            # 正确配置（make_reader_client）下不会发生；万一配置错了，也只是多一次重连，不会失去实时性
            if not state.get("timeout_warned"):
                state["timeout_warned"] = True
                log.warning("event read for run %s timed out on the socket; check reader socket_timeout", run_id)
            return []
        except Exception:
            if not state.get("down_warned"):  # 一次故障期间只告警一次，避免每个轮询周期刷屏
                state["down_warned"] = True
                log.warning("reading events for run %s failed; falling back to db polling", run_id, exc_info=True)
            return None

    async def stream(self, run_id: uuid.UUID, last_event_id: str | None = None) -> AsyncIterator[RunEvent]:
        cursor = last_event_id or "0-0"  # 首次连接从头回放：晚加入的客户端也能看到已生成的部分
        run = await self._runs.find(run_id)
        if run is None:
            return
        # 连接时 run 已结束：不阻塞，回放流里剩下的；流已过期就直接合成，不让客户端白等一个轮询周期
        block = run.status not in _FINISHED and run.status != RunStatus.AWAITING_APPROVAL
        state: dict = {}

        while True:
            events = await self._read(run_id, cursor, block, state)
            if events is None:  # Redis 不可用
                if block:
                    await asyncio.sleep(self._poll_interval)
                events = []
            for event in events:
                cursor = event.id
                if event.type in PAUSE_EVENTS:
                    current = await self._runs.find(run_id)
                    if current is None or current.status != RunStatus.AWAITING_APPROVAL:
                        continue  # 已经答复过的旧审批请求：跳过，接着读后面的事件
                    yield event
                    return
                yield event
                if event.terminal:
                    return
            if events:
                continue  # 还可能有更多，立刻再读

            # 空闲：以数据库为准
            run = await self._runs.find(run_id)
            if run is None:
                return
            if run.status in _FINISHED:
                # 再非阻塞读一次：终态事件可能恰好在“读空”和“查库”之间写入，优先发真实事件
                for event in await self._read(run_id, cursor, False, state) or []:
                    cursor = event.id
                    yield event
                    if event.terminal:
                        return
                yield _synthesize(run)
                return
            if run.status == RunStatus.AWAITING_APPROVAL:
                # 流里没有审批事件（流过期、发布失败）：按数据库合成，客户端照样能看到要审批的内容
                yield RunEvent(RUN_AWAITING_APPROVAL, {"request": run.approval_request or {}, "synthesized": True})
                return
            block = True
