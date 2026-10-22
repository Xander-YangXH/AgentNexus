"""MCP 网关（阶段 4.2，decisions/0021）：工具发现、参数校验、子进程隔离、超时，以及 worker 侧的工具接入。"""

import contextlib
import sys
from pathlib import Path

import httpx
import pytest
from mcp import StdioServerParameters

from agentnexus.agents.tools.mcp import McpToolbox
from agentnexus.mcp_gateway.app import Gateway, create_app, parse_servers

DEMO = str(Path(__file__).parent / "mcp_servers" / "demo.py")


def _gateway(**kw):
    return Gateway({"demo": StdioServerParameters(command=sys.executable, args=[DEMO])}, **kw)


@contextlib.asynccontextmanager
async def running_gateway():
    """真实的网关应用（ASGI，进程内）+ worker 侧的 McpToolbox。上游是真实的 stdio 子进程。

    不写成 async fixture：网关的 lifespan 里开了 anyio 任务组，必须在同一个任务里进入和退出，
    而 pytest-asyncio 的 fixture 准备和清理不在同一个任务里。
    """
    app = create_app(_gateway(call_timeout=2))
    async with app.router.lifespan_context(app), \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as http:
        yield McpToolbox("http://gateway/mcp", http_client=http), app


async def test_lists_upstream_tools():
    async with running_gateway() as (box, app):
        tools = {t.name: t for t in await box.tools()}
        assert set(tools) == {"echo", "env_keys", "boom", "hang"}
        assert tools["echo"].parameters["required"] == ["text"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as c:
            assert (await c.get("/health")).json()["tools"] == ["echo", "env_keys", "boom", "hang"]


async def test_calls_tools_and_reports_errors():
    async with running_gateway() as (box, _):
        tools = {t.name: t for t in await box.tools()}
        assert await tools["echo"].run({"text": "你好"}, None) == "你好"
        for name, args, expected in [("boom", {}, "boom"), ("echo", {}, "text"), ("hang", {}, "timed out")]:
            with pytest.raises(RuntimeError, match=expected):  # 工具报错、参数不合法、超时都以失败返回，网关不挂
                await tools[name].run(args, None)
        assert await tools["echo"].run({"text": "still alive"}, None) == "still alive"


async def test_upstream_does_not_inherit_gateway_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak")
    async with running_gateway() as (box, _):
        keys = (await {t.name: t for t in await box.tools()}["env_keys"].run({}, None)).split(",")
        assert "LLM_API_KEY" not in keys and "DATABASE_URL" not in keys
        assert "PATH" in keys


async def test_unreachable_gateway_yields_no_tools_and_retries_later():
    box = McpToolbox("http://127.0.0.1:9/mcp", retry_seconds=0)
    assert await box.tools() == []  # worker 照常运行，只是没有联网工具
    await box.close()


async def test_duplicate_tool_names_fail_at_startup():
    params = StdioServerParameters(command=sys.executable, args=[DEMO])
    with pytest.raises(RuntimeError, match="provided by both"):
        await Gateway({"a": params, "b": params}).discover()


def test_parse_servers_ignores_env():
    servers = parse_servers('{"fetch": {"command": "python", "args": ["-m", "x"], "env": {"SECRET": "1"}}}')
    assert servers["fetch"].args == ["-m", "x"] and servers["fetch"].env is None
