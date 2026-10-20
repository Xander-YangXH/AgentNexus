"""事件链路（decisions/0007）：发布、读取、派发器的事件顺序、SSE 流的各种收尾路径。"""

import asyncio
import logging
import time
import uuid

import pytest
from redis.asyncio import Redis

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.events import RUN_COMPLETED, RUN_FAILED, RUN_STARTED, TOKEN, RunEvent
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader, make_reader_client, stream_key
from agentnexus.models.fake import FakeEchoChatModel
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus

DEAD_REDIS = "redis://127.0.0.1:1/0"  # 不可达的地址，用来模拟 Redis 故障


async def _conv(sessionmaker, user):
    async with sessionmaker() as session, session.begin():
        return await ConversationRepository(session).create(user.id)


async def _run(sessionmaker, run_id):
    async with sessionmaker() as session:
        return await AgentRunRepository(session).get(run_id)


async def _collect(stream, timeout=5.0) -> list[RunEvent]:
    async def go():
        return [e async for e in stream]

    return await asyncio.wait_for(go(), timeout)


def _types(events):
    return [e.type for e in events]


class FailingModel(FakeEchoChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("llm down")


# ---------- 发布与读取 ----------

async def test_publish_returns_monotonic_ids_and_terminal_sets_ttl(redis):
    pub = RunEventPublisher(redis, ttl_seconds=60)
    run_id = uuid.uuid4()
    a = await pub.publish(run_id, RUN_STARTED, {})
    b = await pub.publish(run_id, TOKEN, {"text": "你"})
    assert a and b and a < b  # 同一毫秒内序号递增，跨毫秒时间戳递增；对本测试的少量写入字典序成立
    assert await redis.ttl(stream_key(run_id)) == -1  # 非终态：不过期
    await pub.publish(run_id, RUN_COMPLETED, {"message_id": "m"})
    assert 0 < await redis.ttl(stream_key(run_id)) <= 60


async def test_reader_resumes_after_cursor_and_preserves_unicode(redis):
    pub, reader = RunEventPublisher(redis), RunEventReader(redis)
    run_id = uuid.uuid4()
    ids = [await pub.publish(run_id, TOKEN, {"text": t}) for t in ["你", "好", "！"]]
    events = await reader.read(run_id, ids[0], block_ms=None)
    assert [e.data["text"] for e in events] == ["好", "！"]
    assert [e.id for e in events] == ids[1:]


async def test_reader_rejects_block_zero_which_means_forever_in_redis(redis):
    with pytest.raises(ValueError):
        await RunEventReader(redis).read(uuid.uuid4(), "0-0", block_ms=0)


async def test_publish_failure_does_not_raise_and_sink_warns_once(caplog):
    dead = Redis.from_url(DEAD_REDIS, decode_responses=True)
    pub = RunEventPublisher(dead)
    run_id = uuid.uuid4()
    assert await pub.publish(run_id, TOKEN, {"text": "x"}) is None
    sink = pub.sink(run_id)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await sink.emit(TOKEN, {"text": "x"})
    assert sum("not being delivered" in r.message for r in caplog.records) == 1
    await dead.aclose()


# ---------- 派发器发出的事件 ----------

def _dispatcher(sessionmaker, redis, model, **kw):
    agent = ChatbotAgent(ChatbotContext(model=model))
    return RunDispatcher(sessionmaker, agent.run, sweep_interval=0.1, events=RunEventPublisher(redis), **kw)


async def test_success_events_order_tokens_and_commit_before_completed(sessionmaker, user, redis):
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "流式输出测试")
    d = _dispatcher(sessionmaker, redis, FakeEchoChatModel(chunk_size=2))
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=0.2)
    await d.start()
    try:
        seen = []
        async for event in streamer.stream(run.id):
            seen.append(event)
            if event.type == RUN_COMPLETED:
                # 收到“完成”的这一刻，数据库里必须已经有这条助手消息（终态事件在提交之后才发）
                got = await _run(sessionmaker, run.id)
                assert got.status == RunStatus.SUCCEEDED
                assert str(got.output_message_id) == event.data["message_id"]
    finally:
        await d.stop()

    types = _types(seen)
    assert types[0] == RUN_STARTED and types[-1] == RUN_COMPLETED
    tokens = [e.data["text"] for e in seen if e.type == TOKEN]
    assert len(tokens) > 1  # 真的是逐段，而不是一整块
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
    assert "".join(tokens) == msgs[-1].content == "echo: 流式输出测试"  # token 拼起来与落库内容一致，没有重复
    assert all(e.id for e in seen) and "synthesized" not in seen[-1].data


async def test_failure_emits_failed_with_same_error_as_db(sessionmaker, user, redis):
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = _dispatcher(sessionmaker, redis, FailingModel())
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=0.2)
    await d.start()
    try:
        seen = await _collect(streamer.stream(run.id))
    finally:
        await d.stop()
    assert _types(seen) == [RUN_STARTED, RUN_FAILED]
    assert seen[-1].data["error"] == (await _run(sessionmaker, run.id)).error == "RuntimeError: llm down"


async def test_startup_recovery_emits_failed_for_orphaned_runs(sessionmaker, user, redis):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    assert await svc.claim(run.id)  # 模拟上一个进程执行到一半被杀
    d = _dispatcher(sessionmaker, redis, FakeEchoChatModel())
    await d.start()
    await d.stop()
    events = await RunEventReader(redis).read(run.id, "0-0", block_ms=None)
    assert _types(events) == [RUN_FAILED] and "restarted" in events[0].data["error"]


async def test_run_still_succeeds_when_redis_is_down(sessionmaker, user):
    dead = Redis.from_url(DEAD_REDIS, decode_responses=True)
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = _dispatcher(sessionmaker, dead, FakeEchoChatModel())
    await d.start()
    try:
        for _ in range(100):
            if (await _run(sessionmaker, run.id)).status == RunStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.05)
        assert (await _run(sessionmaker, run.id)).status == RunStatus.SUCCEEDED  # 事件只是视图
    finally:
        await d.stop()
        await dead.aclose()


# ---------- SSE 流的收尾路径（事件为视图，数据库决定何时结束） ----------

async def _finished_run_with_events(sessionmaker, user, redis):
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "回放")
    d = _dispatcher(sessionmaker, redis, FakeEchoChatModel())
    await d.start()
    try:
        streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=0.2)
        live = await _collect(streamer.stream(run.id))
    finally:
        await d.stop()
    return run, live


async def test_late_joiner_replays_everything_after_run_finished(sessionmaker, user, redis):
    run, live = await _finished_run_with_events(sessionmaker, user, redis)
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=5)
    t0 = time.perf_counter()
    replay = await _collect(streamer.stream(run.id))
    assert replay == live
    assert time.perf_counter() - t0 < 1  # run 已结束：不阻塞等待一个轮询周期


async def test_resume_from_last_event_id(sessionmaker, user, redis):
    run, live = await _finished_run_with_events(sessionmaker, user, redis)
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=5)
    resumed = await _collect(streamer.stream(run.id, last_event_id=live[1].id))
    assert resumed == live[2:]


async def test_expired_stream_is_synthesized_from_db_immediately(sessionmaker, user, redis):
    run, _live = await _finished_run_with_events(sessionmaker, user, redis)
    await redis.delete(stream_key(run.id))  # 模拟流已过期
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=5)
    t0 = time.perf_counter()
    events = await _collect(streamer.stream(run.id))
    assert time.perf_counter() - t0 < 1
    got = await _run(sessionmaker, run.id)
    assert events == [RunEvent(RUN_COMPLETED, {"message_id": str(got.output_message_id), "synthesized": True})]


async def test_lost_terminal_event_is_synthesized_while_waiting(sessionmaker, user, redis):
    """run 在库里变成终态，但终态事件没进流（例如进程崩溃）：订阅方不能永远等下去。"""
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    await svc.claim(run.id)
    await RunEventPublisher(redis).publish(run.id, RUN_STARTED, {})
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=0.2)

    async def fail_silently_later():
        await asyncio.sleep(0.3)
        await svc.fail(run.id, "crashed")  # 只改数据库，不发事件

    asyncio.create_task(fail_silently_later())
    events = await _collect(streamer.stream(run.id), timeout=3)
    assert _types(events) == [RUN_STARTED, RUN_FAILED]
    assert events[-1].data == {"error": "crashed", "synthesized": True}


async def test_streamer_falls_back_to_db_polling_when_redis_is_down(sessionmaker, user):
    dead = Redis.from_url(DEAD_REDIS, decode_responses=True)
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    await svc.claim(run.id)
    streamer = RunEventStreamer(sessionmaker, RunEventReader(dead), poll_interval=0.2)

    async def finish_later():
        await asyncio.sleep(0.3)
        await svc.complete(run.id, conv.id, "done")

    asyncio.create_task(finish_later())
    try:
        events = await _collect(streamer.stream(run.id), timeout=3)
    finally:
        await dead.aclose()
    assert _types(events) == [RUN_COMPLETED] and events[0].data["synthesized"] is True


# ---------- 回归：套接字读超时不能被当成“Redis 不可用” ----------
# 背景（dev-log 问题索引）：redis-py 8 连接默认 socket_timeout=5s，等于 SSE 的阻塞时长；
# 真实模型首字要等 7s 以上，于是每次阻塞读都在套接字上超时，SSE 退化为查库模式，token 直到结束才一次性到达。
# 单元测试没发现，因为测试里的轮询间隔只有 0.2~0.3s，永远碰不到 5s 的套接字超时。

def test_reader_client_socket_timeout_exceeds_block_time():
    client = make_reader_client("redis://localhost:6379/0", block_seconds=5, max_connections=1)
    conn = client.connection_pool.make_connection()  # 只构造对象，不连接
    assert conn.socket_timeout > 5


async def test_socket_timeout_on_read_is_treated_as_idle_not_as_outage(sessionmaker, user, redis):
    """故意配错：套接字超时（0.3s）短于阻塞时长（1s）。token 仍应几乎立刻到达。

    时间线设计：0.3s 时第一次读在套接字上超时。修复前会被当成“Redis 不可用”而睡满 1s（到 1.3s 才再读），
    token 在 0.5s 发出，正好落在这段睡眠里 → 延迟约 0.8s；修复后立即重读 → 延迟接近 0。
    （第一版测试只断言“收到时 run 仍在执行”，在修复前也能通过，变异检查时发现，见 dev-log。）
    """
    misconfigured = Redis.from_url(_url_of(redis), decode_responses=True, socket_timeout=0.3)
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    emitted_at = {}

    async def handler(ctx):
        await asyncio.sleep(0.5)
        emitted_at["t"] = time.perf_counter()
        await ctx.events.emit(TOKEN, {"text": "live"})
        await asyncio.sleep(1.5)
        return "done"

    d = RunDispatcher(sessionmaker, handler, sweep_interval=0.1, events=RunEventPublisher(redis))
    streamer = RunEventStreamer(sessionmaker, RunEventReader(misconfigured), poll_interval=1.0)
    await d.start()
    try:
        async for event in streamer.stream(run.id):
            if event.type == TOKEN:
                latency = time.perf_counter() - emitted_at["t"]
                assert latency < 0.4, f"token 延迟 {latency:.2f}s：读超时被当成了 Redis 故障"
                break
        else:
            raise AssertionError("no token received")
    finally:
        await d.stop()
        await misconfigured.aclose()


def _url_of(redis: Redis) -> str:
    kw = redis.connection_pool.connection_kwargs
    return f"redis://{kw['host']}:{kw['port']}/{kw['db']}"
