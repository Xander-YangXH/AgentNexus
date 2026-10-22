"""测试用的 stdio MCP 服务：回声、报告自己拿到的环境变量、报错、卡住。"""

import os
import sys
import time

from mcp.server.fastmcp import FastMCP

server = FastMCP("demo")


@server.tool()
def echo(text: str) -> str:
    """原样返回 text。"""
    return text


@server.tool()
def env_keys() -> str:
    """子进程能看到哪些环境变量（检查网关有没有把自己的密钥传下去）。"""
    return ",".join(sorted(os.environ))


@server.tool()
def boom() -> str:
    """工具内部出错。"""
    raise ValueError("boom")


@server.tool()
def hang() -> str:
    """一直不返回。"""
    time.sleep(3600)
    return "never"


if __name__ == "__main__":
    print(f"pid {os.getpid()}", file=sys.stderr)
    server.run("stdio")
