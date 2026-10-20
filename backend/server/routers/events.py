import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.sse import EventSourceResponse, ServerSentEvent

from agentnexus.services.run_events import RunEventStreamer
from server.deps import OwnedRun, get_event_streamer

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/runs/{run_id}/events", response_class=EventSourceResponse)
async def run_events(
    run: OwnedRun,
    streamer: RunEventStreamer = Depends(get_event_streamer),
    last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    """订阅一个 run 的事件（SSE）。

    - 事件：run.started / token / run.completed / run.failed，data 为 JSON；
    - 每个事件带 id（Redis 流 ID）；断线后带 `Last-Event-ID` 头重连，从该事件之后继续；
    - 收到终态事件后服务端结束响应；空闲 15 秒框架自动发 `: ping` 保活。
    """
    async for event in streamer.stream(run.id, last_event_id):
        # 用 raw_data 自己序列化：保证中文不被转义成 \uXXXX，前端拿到的就是可读文本
        yield ServerSentEvent(
            event=event.type, id=event.id, raw_data=json.dumps(event.data, ensure_ascii=False)
        )
