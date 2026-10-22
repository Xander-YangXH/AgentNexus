"""SSE 的 HTTP 层测试。

必须起真实的 uvicorn：httpx 的 ASGITransport 会把整个响应缓冲完才返回（已实测：三条间隔 0.5s 的事件
在 1.5s 时同时到达），无法测试“逐条到达”和“客户端中途断开”。
"""

import asyncio
import json
import uuid

import httpx
import pytest_asyncio
import uvicorn
from redis.asyncio import Redis

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader, make_reader_client
from agentnexus.models.fake import FakeEchoChatModel
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.run_events import RunEventStreamer
from server.main import create_app

SSE_CLIENT_NAME = "sse-test-reader"


async def _start_server(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}"


@pytest_asyncio.fixture
async def make_live(sessionmaker, redis):
    """起一个真实 HTTP 服务，handler 可替换；返回 (client, redis)。"""
    started = []

    async def factory(handler=None):
        # client_name 便于在 Redis 侧用 CLIENT LIST 观察 SSE 读连接
        sse_redis = make_reader_client(
            _redis_url(redis), block_seconds=0.3, max_connections=10, client_name=SSE_CLIENT_NAME
        )
        if handler is None:
            # 默认：假模型逐块输出，每块间隔 0.05s
            handler = ChatbotAgent(ChatbotContext(model=FakeEchoChatModel(chunk_size=2, stream_delay=0.05))).run
        app = create_app(with_lifespan=False)
        dispatcher = RunDispatcher(sessionmaker, handler, sweep_interval=0.1, events=RunEventPublisher(redis))
        app.state.sessionmaker = sessionmaker
        app.state.dispatcher = dispatcher
        app.state.event_streamer = RunEventStreamer(sessionmaker, RunEventReader(sse_redis), poll_interval=0.3)
        await dispatcher.start()
        server, task, base = await _start_server(app)
        client = httpx.AsyncClient(base_url=base, timeout=10)
        started.append((client, server, task, dispatcher, sse_redis))
        return client, redis

    yield factory
    for client, server, task, dispatcher, sse_redis in started:
        await client.aclose()
        server.should_exit = True
        await task
        await dispatcher.stop()
        await sse_redis.aclose()


@pytest_asyncio.fixture
async def live(make_live):
    return await make_live()


def _redis_url(redis: Redis) -> str:
    kw = redis.connection_pool.connection_kwargs
    return f"redis://{kw['host']}:{kw['port']}/{kw['db']}"


async def _user(client, name=None):
    r = await client.post("/api/users", json={"name": name or f"u-{uuid.uuid4().hex[:8]}"})
    return {"X-User-Id": r.json()["id"]}


async def _send(client, headers, text):
    cid = (await client.post("/api/conversations", json={}, headers=headers)).json()["id"]
    r = await client.post(f"/api/conversations/{cid}/messages", json={"content": text}, headers=headers)
    assert r.status_code == 202
    return r.json()["id"]


async def _read_sse(response, stop_after=None):
    """把 SSE 字节流解析成事件列表；注释行（如 `: ping`）跳过。"""
    events, current = [], {}
    async for line in response.aiter_lines():
        if line == "":
            if current:
                events.append(current)
                current = {}
                if stop_after and len(events) >= stop_after:
                    break
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        current[field] = value
    return events


async def test_sse_wire_format_and_full_flow(live):
    client, _ = live
    h = await _user(client)
    run_id = await _send(client, h, "你好，流式")
    async with client.stream("GET", f"/api/runs/{run_id}/events", headers=h) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-request-id"]  # 经过请求上下文中间件后仍是流式响应，且带 request id
        events = await _read_sse(r)

    assert events[0]["event"] == "run.started" and events[-1]["event"] == "run.completed"
    assert all("id" in e for e in events)
    tokens = [json.loads(e["data"])["text"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "echo: 你好，流式"
    assert any("你" in e["data"] for e in events)  # 中文没有被转义成 \uXXXX


async def test_sse_ownership_is_checked_before_streaming(live):
    client, _ = live
    alice, bob = await _user(client), await _user(client)
    run_id = await _send(client, alice, "secret")
    r = await client.get(f"/api/runs/{run_id}/events", headers=bob)
    assert r.status_code == 404  # 不是先返回 200 再在流里报错
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/events", headers=alice)).status_code == 404
    assert (await client.get(f"/api/runs/{run_id}/events")).status_code == 401


async def test_sse_resume_with_last_event_id_header(live):
    client, _ = live
    h = await _user(client)
    run_id = await _send(client, h, "续传")
    async with client.stream("GET", f"/api/runs/{run_id}/events", headers=h) as r:
        full = await _read_sse(r)
    async with client.stream(
        "GET", f"/api/runs/{run_id}/events", headers={**h, "Last-Event-ID": full[1]["id"]}
    ) as r:
        resumed = await _read_sse(r)
    assert [e["id"] for e in resumed] == [e["id"] for e in full[2:]]


async def _blocked_sse_readers(redis: Redis) -> int:
    """Redis 侧还处于阻塞读（flags 含 b）状态、且属于 SSE 读客户端的连接数。"""
    clients = await redis.client_list()
    return sum(1 for c in clients if c.get("name") == SSE_CLIENT_NAME and "b" in c.get("flags", ""))


async def test_client_disconnect_releases_blocked_redis_reader(make_live):
    async def stalled(ctx):
        await asyncio.sleep(30)  # 发完 run.started 后长时间不出数据：读取方会一直阻塞在 Redis 上
        return "never"

    client, redis = await make_live(stalled)
    h = await _user(client)
    run_id = await _send(client, h, "q")
    async with client.stream("GET", f"/api/runs/{run_id}/events", headers=h) as r:
        first = await _read_sse(r, stop_after=1)
        assert first[0]["event"] == "run.started"
        # 先证明测量手段有效：断开前确实能观察到 1 个为该客户端阻塞的 Redis 连接。
        # 读取方每 0.3s 超时一次、查库后再阻塞，阻塞状态会短暂消失，所以要断言“循环里观察到的值”，
        # 不能循环结束后再查一次（那次查询可能恰好落在空隙里——这里曾因此不稳定，见 dev-log 问题索引）
        observed, seen = 0, []
        for _ in range(30):
            observed = await _blocked_sse_readers(redis)
            seen.append(observed)
            if observed == 1:
                break
            await asyncio.sleep(0.05)
        assert observed == 1, seen
    # 退出 with = 客户端断开。之后连续采样 1 秒都必须是 0：服务端已取消读取并释放连接
    await asyncio.sleep(0.3)
    samples = []
    for _ in range(10):
        samples.append(await _blocked_sse_readers(redis))
        await asyncio.sleep(0.1)
    assert samples == [0] * 10
