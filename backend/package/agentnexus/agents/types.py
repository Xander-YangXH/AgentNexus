"""Agent 面向运行时的输入契约。服务层构造它，Agent 实现消费它；Agent 不依赖数据库。"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


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


# 一个 Agent 就是“给定上下文，返回助手回复文本”的异步函数（流式在 1.5 再扩展）
RunHandler = Callable[[RunContext], Awaitable[str]]
