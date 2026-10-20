"""Agent 面向运行时的输入契约。服务层构造它，Agent 实现消费它；Agent 不依赖数据库。"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agentnexus.events import NULL_SINK, EventSink


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class RunContext:
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    # 截止到本次 run 的输入消息（含）的历史，按 seq 排序
    history: list[ChatMessage]
    # 执行中发布事件（如 token）的出口。默认什么都不做，所以不关心流式的 Agent 和测试无需改动
    events: EventSink = NULL_SINK


# 一个 Agent 就是“给定上下文，返回助手回复文本”的异步函数。
# 流式输出不改变这个签名：Agent 边执行边 ctx.events.emit(...)，最终仍返回完整文本，由派发器落库（decisions/0007）
RunHandler = Callable[[RunContext], Awaitable[str]]
