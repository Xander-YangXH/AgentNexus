"""worker 侧的 MCP 工具（decisions/0021）：经 MCP 网关（streamable HTTP）使用第三方 MCP 服务。

直接用 mcp SDK 的客户端：每次列工具、调用工具都新建一个会话（网关是无状态的，内网一次往返几毫秒），
网关重启、升级不需要 worker 做任何“重连”。

网关不可用时不影响 worker 启动和普通对话：拿不到工具就当没有联网工具（依赖它的 skill 也会被隐藏），
过 retry_seconds 后再试。
"""

import asyncio
import contextlib
import logging
import time

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentnexus.agents.tools import AgentTool, ToolError, ToolScope, truncate

log = logging.getLogger(__name__)


class McpToolbox:
    def __init__(self, url: str, *, http_client: httpx.AsyncClient | None = None, retry_seconds: float = 60.0,
                 timeout: float = 90.0):
        self._url = url
        # 抓网页可能要几十秒（网关那边单次调用上限 60 秒），读超时要比它长；连接超时短：连不上就快速放弃
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0))
        self._retry_seconds = retry_seconds
        self._tools: list[AgentTool] | None = None
        self._last_attempt = float("-inf")
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    @contextlib.asynccontextmanager
    async def _session(self):
        async with streamable_http_client(self._url, http_client=self._http) as (read, write, _), \
                ClientSession(read, write) as session:
            await session.initialize()
            yield session

    async def tools(self) -> list[AgentTool]:
        if self._tools is not None:
            return self._tools
        async with self._lock:
            if self._tools is not None or time.monotonic() - self._last_attempt < self._retry_seconds:
                return self._tools or []
            self._last_attempt = time.monotonic()
            try:
                async with self._session() as session:
                    listing = await session.list_tools()
            except Exception as exc:
                log.warning("MCP gateway unavailable (%s: %s); continuing without MCP tools", type(exc).__name__, exc)
                return []
            self._tools = [self._wrap(t) for t in listing.tools]
            log.info("loaded %d MCP tool(s): %s", len(self._tools), ", ".join(t.name for t in self._tools))
            return self._tools

    def _wrap(self, tool: types.Tool) -> AgentTool:
        async def run(args: dict, scope: ToolScope | None) -> str:
            try:
                async with self._session() as session:
                    result = await session.call_tool(tool.name, args)
            except Exception as exc:
                log.warning("MCP call %s failed", tool.name, exc_info=True)
                raise ToolError(f"工具 {tool.name} 暂时不可用（{type(exc).__name__}）") from None
            text = _text(result)
            if result.isError:
                raise ToolError(truncate(text, 1000))
            return truncate(text)

        return AgentTool(tool.name, tool.description or "", tool.inputSchema, run)


def _text(result: types.CallToolResult) -> str:
    parts = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:  # 图片、音频等：目前的模型调用只传文字，给个占位说明
            parts.append(f"[{block.type} 内容，已省略]")
    return "\n".join(parts)
