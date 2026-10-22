"""Agent 的通用工具层（阶段 4.2，decisions/0021）。

一个工具 = 名称 + 描述 + 参数的 JSON Schema + 异步执行函数。来源有三种：
- 沙盒工具（sandbox.py）：python_exec、文件读写，经 sandbox 服务在一次性容器里执行；
- MCP 工具（mcp.py）：经 MCP 网关调用第三方 MCP 服务（如 fetch）；
- knowledge_search 不在这里：它要产出引用（citations），由图里的专门逻辑处理（decisions/0018）。

工具执行时拿到的 ToolScope（在哪个工作空间、以谁的身份）由服务端决定，不在模型给的参数里——
否则提示注入就能让工具去读别的对话的文件。
"""

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

OUTPUT_LIMIT = 8000  # 工具结果最多给模型多少字符：超大输出（打印整张表、整个网页）会挤爆上下文、白花 token


class ToolError(RuntimeError):
    """工具执行失败。消息会原样告诉模型（不要放内部细节）。"""


@dataclasses.dataclass(frozen=True)
class ToolScope:
    workspace_id: str  # 文件区：当前对话的 id（同一对话的多轮回答共用文件）
    user_id: str | None = None


@dataclasses.dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    run: Callable[[dict, ToolScope | None], Awaitable[str]]

    def spec(self) -> dict:
        """OpenAI 函数格式；各供应商的 bind_tools 都能转换。"""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class ToolSource(Protocol):
    async def tools(self) -> list[AgentTool]: ...


class StaticTools:
    def __init__(self, tools: Sequence[AgentTool]):
        self._tools = list(tools)

    async def tools(self) -> list[AgentTool]:
        return self._tools


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（输出过长，已截断，共 {len(text)} 字符；需要时请只打印关键部分）"
