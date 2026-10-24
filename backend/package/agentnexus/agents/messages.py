"""我们的消息（数据库里的 role + content）与 LangChain 消息的互转。

这是两个世界的边界：数据库不认识 LangChain 类型，图也不认识我们的表。
"""

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agentnexus.agents.types import ChatMessage


def to_langchain(history: list[ChatMessage]) -> list[AnyMessage]:
    out: list[AnyMessage] = []
    for m in history:
        if m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        elif m.role == "system":
            out.append(SystemMessage(content=m.content))
        elif m.role == "tool":
            # 历史里的 tool 消息缺少 tool_call_id，无法还原成合法的 ToolMessage；
            # 按决策 0006，工具中间结果不入 messages 表，这里若出现说明数据异常，直接报错而不是静默丢弃
            raise ValueError("messages 表里不应出现 tool 角色（见 decisions/0006）")
        else:
            raise ValueError(f"unknown role: {m.role!r}")
    return out


def message_text(message: AnyMessage) -> str:
    """取消息的纯文本。部分供应商（如 Anthropic）的 content 是内容块列表，不是字符串。"""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") if isinstance(block, dict) else str(block)
        for block in content
        if not isinstance(block, dict) or block.get("type") == "text"
    )
