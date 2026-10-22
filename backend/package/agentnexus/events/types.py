"""运行事件的契约（decisions/0007）。Agent、派发器、SSE 三方共用，不依赖任何传输层。"""

from dataclasses import dataclass, field
from typing import Any, Protocol

RUN_STARTED = "run.started"
TOKEN = "token"  # data: {"text": "..."}
RUN_COMPLETED = "run.completed"  # data: {"message_id": "..."}
RUN_FAILED = "run.failed"  # data: {"error_code": "..."}（错误码见 runtime/failures.py；原始报错不对外）
RUN_CANCELLED = "run.cancelled"  # data: {}
RUN_RESUMED = "run.resumed"  # data: {"attempt": 2}——worker 被打断后由别的 worker 接管续做（decisions/0020）；之后的 token 带 attempt
TOOL_STARTED = "tool.started"  # data: {"name": "knowledge_search", "query": "..."}（decisions/0018）
TOOL_FINISHED = "tool.finished"  # data: {"name": "knowledge_search", "hits": 4}
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
