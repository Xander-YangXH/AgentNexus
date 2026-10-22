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
    # 对话的归属用户与关联的知识库（decisions/0018）。工具只能检索这些知识库，由服务端决定，不由模型指定
    user_id: uuid.UUID | None = None
    knowledge_base_ids: tuple[uuid.UUID, ...] = ()
    # 第几次执行（decisions/0020）：大于 1 表示前一个 worker 被打断、由本次接管，Agent 应从 checkpoint 续做
    attempt: int = 1
    # 对话的 Agent 类型（decisions/0022）：chat / research，决定由哪个 Agent 执行
    agent: str = "chat"
    # 用户对审批的答复（decisions/0022）。不为 None 表示这次执行是“批准 / 修改意见之后”的继续
    approval: dict | None = None


@dataclass(frozen=True)
class AgentReply:
    """Agent 的结果：回复正文 + 引用的知识库片段快照（没有检索时为 None）。只返回正文的 Agent 直接返回 str 即可。"""

    content: str
    citations: list[dict] | None = None
    # 产物（decisions/0022）：[{id, filename, content_type, size, blob_key}]，原件已由 Agent 写入原件存储
    attachments: list[dict] | None = None


@dataclass(frozen=True)
class ApprovalRequired:
    """Agent 需要用户审批才能继续（decisions/0022）。request 会展示给用户（例如调研计划）。

    派发器收到它后把 run 置为 awaiting_approval 并放弃租约；批准后 run 回到 queued，Agent 从 checkpoint 继续。
    """

    request: dict


# 一个 Agent 就是“给定上下文，返回助手回复”的异步函数：返回文本，或带引用的 AgentReply（decisions/0018）。
# 流式输出不改变这个签名：Agent 边执行边 ctx.events.emit(...)，最终仍返回完整结果，由派发器落库（decisions/0007）
RunHandler = Callable[[RunContext], Awaitable["str | AgentReply | ApprovalRequired"]]
