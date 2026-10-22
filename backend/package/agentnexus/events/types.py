"""运行事件的契约（decisions/0007）。Agent、派发器、SSE 三方共用，不依赖任何传输层。"""

from dataclasses import dataclass, field
from typing import Any, Protocol

RUN_STARTED = "run.started"
TOKEN = "token"  # data: {"text": "..."}
RUN_COMPLETED = "run.completed"  # data: {"message_id": "..."}
RUN_FAILED = "run.failed"  # data: {"error_code": "..."}（错误码见 runtime/failures.py；原始报错不对外）
RUN_CANCELLED = "run.cancelled"  # data: {}
RUN_RESUMED = "run.resumed"  # data: {"attempt": 2}——worker 被打断后由别的 worker 接管续做（decisions/0020）；之后的 token 带 attempt
# data: {"name": "knowledge_search", "query": "...", "detail": "..."}（decisions/0018；detail 是参数摘要，0021 起所有工具都有）
TOOL_STARTED = "tool.started"
TOOL_FINISHED = "tool.finished"  # data: {"name": "python_exec", "ok": true, "hits": 0}（hits 只对 knowledge_search 有意义）
# data: {"request": {...}}（decisions/0022）。等待用户审批：这次执行结束、不占 worker；SSE 流在这里暂停（结束响应），
# 用户批准后客户端带 Last-Event-ID 重新订阅，后续事件接着发。不是终态
RUN_AWAITING_APPROVAL = "run.awaiting_approval"
# data: {"index": 0, "title": "...", "status": "started" | "finished" | "failed", "total": 3}（decisions/0022）
RESEARCH_SUBTASK = "research.subtask"
TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})
PAUSE_EVENTS = frozenset({RUN_AWAITING_APPROVAL})


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
