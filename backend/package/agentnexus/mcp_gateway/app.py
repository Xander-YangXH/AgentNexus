"""MCP 网关（阶段 4.2，decisions/0021）：把配置好的 stdio MCP 服务，以 streamable HTTP 提供给 worker。

为什么要有这一层：第三方 MCP 服务是别人写的代码，又会访问模型指定的地址。把它们放进一个只接入 internal 网络、
只能经出口代理（Squid）出网的容器里：即使服务有漏洞或被投毒，也碰不到内部服务（Redis / Qdrant / Neo4j 都没有鉴权）
和 worker 的文件、密钥。

- 每次工具调用都新起一个子进程（用完即退）：调用之间不共享任何状态，一个用户的调用影响不到下一个用户；
  上游进程崩了也不需要“重连”。代价是每次多一次进程启动（实测见 0021 的实现记录）。
- 子进程只拿到 mcp SDK 默认的几个环境变量（HOME、PATH 等），网关自己的环境变量不会传下去。
- 工具列表在启动时问一遍各上游并缓存；工具名必须唯一（重名直接启动失败，不悄悄覆盖）。
- 本模块只依赖 mcp / starlette / uvicorn：网关镜像不装后端的其他代码。

配置：环境变量 MCP_SERVERS（JSON），例如
  {"fetch": {"command": "python", "args": ["-m", "mcp_server_fetch", "--proxy-url", "http://egress-proxy:3128"]}}
"""

import contextlib
import json
import logging
import os

import anyio
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("agentnexus.mcp_gateway")


class Gateway:
    def __init__(self, upstreams: dict[str, StdioServerParameters], *, call_timeout: float = 60.0, concurrency: int = 4):
        self.upstreams = upstreams
        self.call_timeout = call_timeout
        self._slots = anyio.Semaphore(concurrency)  # 每个调用一个子进程，限制同时存在的个数
        self.tools: list[types.Tool] = []
        self._owner: dict[str, str] = {}  # 工具名 -> 上游名

    @contextlib.asynccontextmanager
    async def _session(self, name: str):
        async with stdio_client(self.upstreams[name]) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield session

    async def discover(self) -> None:
        tools, owner = [], {}
        for name in self.upstreams:
            async with self._session(name) as session:
                listing = (await session.list_tools()).tools
            # 检查放在会话外面：在会话（anyio 任务组）里抛出的异常会被包成 ExceptionGroup
            for tool in listing:
                if tool.name in owner:
                    raise RuntimeError(f"tool {tool.name!r} is provided by both {owner[tool.name]!r} and {name!r}")
                owner[tool.name] = name
                tools.append(tool)
        self.tools, self._owner = tools, owner
        log.info("discovered %d tool(s): %s", len(tools), ", ".join(f"{t}@{s}" for t, s in owner.items()))

    async def call(self, tool: str, arguments: dict) -> types.CallToolResult:
        server = self._owner.get(tool)
        if server is None:
            return _error(f"unknown tool: {tool}")
        async with self._slots:
            try:
                with anyio.fail_after(self.call_timeout):
                    async with self._session(server) as session:
                        return await session.call_tool(tool, arguments)
            except TimeoutError:
                return _error(f"tool {tool} timed out after {self.call_timeout:g}s")
            except Exception as exc:  # 上游启动失败、协议错误等：告诉调用方失败了，不让网关挂掉
                log.warning("calling %s on %s failed", tool, server, exc_info=True)
                return _error(f"tool {tool} failed: {type(exc).__name__}")


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def create_app(gateway: Gateway) -> Starlette:
    server = Server("agentnexus-mcp-gateway")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return gateway.tools

    # 参数先按上游声明的 inputSchema 校验（validate_input），不合法的调用到不了第三方代码
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        return await gateway.call(name, arguments)

    # 无状态：每个 HTTP 请求独立处理，网关重启不影响 worker（worker 每次调用也新建会话）
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)

    class McpEndpoint:  # ASGI 应用（不是函数），Starlette 的 Route 会把请求原样交给它
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    async def health(request):
        return JSONResponse({"status": "ok", "tools": [t.name for t in gateway.tools]})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await gateway.discover()  # 上游起不来就启动失败：比带着残缺的工具列表运行更容易发现
        async with manager.run():
            yield

    return Starlette(routes=[Route("/health", health), Route("/mcp", McpEndpoint(), methods=["GET", "POST", "DELETE"])],
                     lifespan=lifespan)


def parse_servers(raw: str) -> dict[str, StdioServerParameters]:
    servers = {}
    for name, spec in json.loads(raw).items():
        # 不接受配置里的 env：子进程只拿 SDK 默认的那几个环境变量。确实需要密钥的服务，以后单独评估再放开
        servers[name] = StdioServerParameters(command=spec["command"], args=list(spec.get("args", [])))
    return servers


def _from_env() -> Starlette:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    gateway = Gateway(parse_servers(os.environ["MCP_SERVERS"]),
                      call_timeout=float(os.environ.get("MCP_CALL_TIMEOUT", 60)),
                      concurrency=int(os.environ.get("MCP_CONCURRENCY", 4)))
    return create_app(gateway)


app = _from_env() if os.environ.get("MCP_SERVERS") else None  # 测试里直接调用 create_app
