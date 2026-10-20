"""运行事件的契约（decisions/0007）。Agent、派发器、SSE 三方共用，不依赖任何传输层。"""

from dataclasses import dataclass, field
from typing import Any, Protocol

RUN_STARTED = "run.started"
TOKEN = "token"  # data: {"text": "..."}
RUN_COMPLETED = "run.completed"  # data: {"message_id": "..."}
RUN_FAILED = "run.failed"  # data: {"error": "..."}
RUN_CANCELLED = "run.cancelled"  # 预留：取消 run 还未实现
TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})


@dataclass(frozen=True)
class RunEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    # Redis 流 ID，同时是 SSE 的 id（客户端续传用）。由数据库状态合成的事件没有 id
    id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS


class EventSink(Protocol):
    """Agent 在执行中发布事件的接口。实现必须“发不出去也不抛错”：事件只是视图。"""

    async def emit(self, type: str, data: dict[str, Any]) -> None: ...


class _NullSink:
    async def emit(self, type: str, data: dict[str, Any]) -> None:
        return None


NULL_SINK: EventSink = _NullSink()
