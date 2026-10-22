"""MCP 选型实验（decisions/0021 草稿）：用 langchain-mcp-adapters 通过 stdio 接入官方参考实现 mcp-server-fetch。

看三件事：工具描述长什么样、正常抓取的耗时、以及 SSRF——如果让它在 worker 所在的网络里运行，
模型（或被提示注入的文档）可以让它去请求内部服务。这里用本机暴露的 Qdrant 端口模拟“内部服务”。
用法：pip install langchain-mcp-adapters "mcp<2" mcp-server-fetch；python fetch_stdio.py
"""

import asyncio
import sys
import time

from langchain_mcp_adapters.client import MultiServerMCPClient


async def main():
    client = MultiServerMCPClient({
        "fetch": {"command": sys.executable, "args": ["-m", "mcp_server_fetch", "--ignore-robots-txt"], "transport": "stdio"},
    })
    t = time.perf_counter()
    tools = await client.get_tools()
    print(f"连接并列出工具：{(time.perf_counter() - t) * 1000:.0f}ms")
    for tool in tools:
        print(f"- {tool.name}: {tool.description[:90]}…\n  参数：{list(tool.args)}")
    fetch = tools[0]
    for url in ("https://example.com", "http://localhost:6333/collections"):
        t = time.perf_counter()
        try:
            out = await fetch.ainvoke({"url": url, "max_length": 300})
            text = out if isinstance(out, str) else str(out)
            print(f"\n抓取 {url}（{(time.perf_counter() - t) * 1000:.0f}ms）：{text[:200]!r}")
        except Exception as exc:
            print(f"\n抓取 {url} 失败：{type(exc).__name__}: {str(exc)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
