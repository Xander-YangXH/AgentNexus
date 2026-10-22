"""run 的生命周期补全（decisions/0009）：错误分类、取消、重试。"""

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.agents.echo import echo_agent
from agentnexus.events import RUN_CANCELLED, RUN_FAILED, RUN_STARTED
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import CancelOutcome, RunDispatcher
from agentnexus.runtime.failures import ErrorCode, classify
from agentnexus.services.errors import RunConflict, RunNotRetryable
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app


# ---------- 错误分类 ----------

class _WithStatus(Exception):
    def __init__(self, status_code=None, code=None, status=None):
        super().__init__("boom")
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_WithStatus(status_code=429), ErrorCode.LLM_RATE_LIMITED),
        (_WithStatus(status_code=503), ErrorCode.LLM_UNAVAILABLE),
        (_WithStatus(code=500), ErrorCode.LLM_UNAVAILABLE),
        (_WithStatus(status="RESOURCE_EXHAUSTED"), ErrorCode.LLM_RATE_LIMITED),
        (_WithStatus(status_code=401), ErrorCode.LLM_CONFIG),
        (_WithStatus(code="E123"), ErrorCode.INTERNAL),  # 字符串 code 不能误认为 HTTP 状态
        (_WithStatus(code=True), ErrorCode.INTERNAL),  # bool 是 int 的子类，也要排除
        (ValueError("bad"), ErrorCode.INTERNAL),
    ],
)
def test_classify(exc, expected):
    assert classify(exc) == expected


def test_classify_follows_exception_chain_like_gemini_wrapping():
    """Gemini：LangChain 的包装异常本身不带状态码，状态码在 __cause__ 上（已在容器里实测这种结构）。"""
    try:
        try:
            raise _WithStatus(code=503, status="UNAVAILABLE")
        except Exception as inner:
            raise RuntimeError("GoogleAPIError: 503 UNAVAILABLE") from inner
    except RuntimeError as outer:
        assert classify(outer) == ErrorCode.LLM_UNAVAILABLE


def test_classify_survives_cyclic_exception_chain():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert classify(a) == ErrorCode.INTERNAL


# ---------- 公共工具 ----------

async def _conv(sessionmaker, user):
    async with sessionmaker() as session, session.begin():
        return await ConversationRepository(session).create(user.id)


async def _run(sessionmaker, run_id):
    async with sessionmaker() as session:
        return await AgentRunRepository(session).get(run_id)


async def _wait_status(sessionmaker, run_id, *statuses, timeout=5.0):
    async def poll():
        while (r := await _run(sessionmaker, run_id)).status not in statuses:
            await asyncio.sleep(0.02)
        return r

    return await asyncio.wait_for(poll(), timeout)


async def _events(redis, run_id):
    return [e.type for e in await RunEventReader(redis).read(run_id, "0-0", block_ms=None)]


# ---------- 失败时记录错误码 ----------

async def test_failure_records_error_code_and_publishes_only_the_code(sessionmaker, user, redis):
    async def overloaded(ctx):
        raise _WithStatus(status_code=503)

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, overloaded, sweep_interval=0.1, events=RunEventPublisher(redis))
    await d.start()
    try:
        got = await _wait_status(sessionmaker, run.id, RunStatus.FAILED)
    finally:
        await d.stop()
    assert got.error_code == "llm_unavailable" and "_WithStatus" in got.error  # 原始报错留在库里
    events = await RunEventReader(redis).read(run.id, "0-0", block_ms=None)
    assert events[-1].type == RUN_FAILED and events[-1].data == {"error_code": "llm_unavailable"}


async def test_timeout_and_shutdown_have_their_own_codes(sessionmaker, user):
    async def slow(ctx):
        await asyncio.sleep(30)

    svc = RunService(sessionmaker)
    conv1, conv2 = await _conv(sessionmaker, user), await _conv(sessionmaker, user)
    timed = await svc.submit(user.id, conv1.id, "q")
    d = RunDispatcher(sessionmaker, slow, sweep_interval=0.1, run_timeout=0.2)
    await d.start()
    try:
        assert (await _wait_status(sessionmaker, timed.id, RunStatus.FAILED)).error_code == "timeout"
        stopped = await svc.submit(user.id, conv2.id, "q")
        await _wait_status(sessionmaker, stopped.id, RunStatus.RUNNING)
    finally:
        await d.stop()
    got = await _run(sessionmaker, stopped.id)
    # 关停不是“用户取消”，也不再标失败（decisions/0020）：放弃租约，留给别的 worker 接管续做
    assert (got.status, got.error_code, got.cancel_requested_at) == (RunStatus.RUNNING, None, None)


# ---------- 取消 ----------

async def test_cancel_queued_run(sessionmaker, user, redis):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, echo_agent, events=RunEventPublisher(redis))  # 不 start：run 保持排队
    assert await d.cancel(run.id) == CancelOutcome.CANCELLED
    got = await _run(sessionmaker, run.id)
    assert got.status == RunStatus.CANCELLED and got.finished_at is not None
    assert await _events(redis, run.id) == [RUN_CANCELLED]
    await svc.submit(user.id, conv.id, "next")  # 对话已释放


async def test_cancel_running_run_stops_the_agent(sessionmaker, user, redis):
    started, was_cancelled = asyncio.Event(), asyncio.Event()

    async def long_task(ctx):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            was_cancelled.set()
            raise
        return "never"

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, long_task, sweep_interval=0.1, events=RunEventPublisher(redis))
    await d.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert await d.cancel(run.id) == CancelOutcome.REQUESTED
        got = await _wait_status(sessionmaker, run.id, RunStatus.CANCELLED, RunStatus.FAILED)
        assert got.status == RunStatus.CANCELLED and got.error_code is None
        assert was_cancelled.is_set()  # Agent 确实被打断，而不只是改了状态
        # worker 循环没有被连带取消：还能继续处理新的 run
        nxt = await RunService(sessionmaker).submit(user.id, conv.id, "next")
        d._handler = echo_agent
        assert (await _wait_status(sessionmaker, nxt.id, RunStatus.SUCCEEDED)).status == RunStatus.SUCCEEDED
    finally:
        await d.stop()
    assert await _events(redis, run.id) == [RUN_STARTED, RUN_CANCELLED]
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
    assert [m.content for m in msgs] == ["q", "next", "echo: next"]  # 被取消的 run 没有留下助手消息


async def test_cancel_arriving_before_the_agent_task_exists(sessionmaker, user):
    """空档：run 已被认领（running），但执行 Agent 的任务还没创建。取消请求不能丢。"""
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    called = asyncio.Event()

    async def handler(ctx):
        called.set()
        return "should not run"

    d = RunDispatcher(sessionmaker, handler, sweep_interval=0.1)
    real_load = d._svc.load_context
    in_window = asyncio.Event()

    async def slow_load(run_id):
        in_window.set()
        await asyncio.sleep(0.3)  # 拉长“认领之后、任务创建之前”的空档
        return await real_load(run_id)

    d._svc.load_context = slow_load
    await d.start()
    try:
        await asyncio.wait_for(in_window.wait(), 3)
        assert await d.cancel(run.id) == CancelOutcome.REQUESTED
        got = await _wait_status(sessionmaker, run.id, RunStatus.CANCELLED, RunStatus.SUCCEEDED)
    finally:
        await d.stop()
    assert got.status == RunStatus.CANCELLED
    # 任务在开始运行前就被取消：Agent 的函数体一行都没有执行
    assert not called.is_set()


async def test_user_cancel_during_shutdown_does_not_hang_stop(sessionmaker, user):
    """用户取消与服务关停同时发生：必须按“关停”处理并让 stop() 正常返回。

    如果把所有 CancelledError 都当成用户取消并吞掉，worker 任务收到的那次关停取消就被消耗了，
    之后它回到 queue.get() 上永远等下去，stop() 会挂住（纯 asyncio 的取消只投递一次）。
    """
    started = asyncio.Event()

    async def long_task(ctx):
        started.set()
        await asyncio.sleep(30)

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, long_task, sweep_interval=0.1)
    await d.start()
    await asyncio.wait_for(started.wait(), 3)
    await d.cancel(run.id)  # 用户取消
    await asyncio.wait_for(d.stop(), 3)  # 紧接着关停：不能挂住
    got = await _run(sessionmaker, run.id)
    assert got.status in (RunStatus.CANCELLED, RunStatus.RUNNING)  # 取消先生效，或者关停先生效（放弃了租约）
    assert got.cancel_requested_at is not None  # 取消请求已记在数据库里
    # 关停先生效的情况：接手的 worker 看到取消请求，直接收尾为已取消，不再执行
    executed = []
    d2 = RunDispatcher(sessionmaker, lambda ctx: executed.append(1), sweep_interval=0.1)
    await d2.start()
    try:
        got = await _wait_status(sessionmaker, run.id, RunStatus.CANCELLED)
    finally:
        await d2.stop()
    assert executed == []


async def test_cancel_finished_run_reports_finished(sessionmaker, user):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    await svc.claim(run.id)
    await svc.complete(run.id, conv.id, "done")
    d = RunDispatcher(sessionmaker, echo_agent)
    assert await d.cancel(run.id) == CancelOutcome.FINISHED
    assert (await _run(sessionmaker, run.id)).status == RunStatus.SUCCEEDED  # 没被改写


# ---------- 重试 ----------

async def test_retry_reuses_the_input_message(sessionmaker, user):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    await svc.claim(run.id)
    await svc.fail(run.id, "x", ErrorCode.LLM_UNAVAILABLE)

    new = await svc.retry(user.id, run.id)
    assert new.id != run.id and new.input_message_id == run.input_message_id and new.status == RunStatus.QUEUED
    async with sessionmaker() as session:
        assert len(await MessageRepository(session).list_by_conversation(conv.id)) == 1  # 没有重复的用户消息
    with pytest.raises(RunConflict):
        await svc.retry(user.id, run.id)  # 新的 run 还在排队：同一对话不能有两个活跃 run


async def test_retry_rules(sessionmaker, user):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    first = await svc.submit(user.id, conv.id, "q1")
    await svc.claim(first.id)
    await svc.complete(first.id, conv.id, "a1")
    with pytest.raises(RunNotRetryable):
        await svc.retry(user.id, first.id)  # 成功的不能重试

    second = await svc.submit(user.id, conv.id, "q2")
    await svc.claim(second.id)
    await svc.fail(second.id, "x")
    third = await svc.submit(user.id, conv.id, "q3")  # 失败后用户直接发了新消息
    await svc.claim(third.id)
    await svc.fail(third.id, "x")
    with pytest.raises(RunNotRetryable):
        await svc.retry(user.id, second.id)  # 已经不是最后一条消息
    await svc.retry(user.id, third.id)  # 最后一条可以


# ---------- HTTP 接口 ----------

@pytest_asyncio.fixture
async def api(sessionmaker):
    app = create_app(with_lifespan=False)
    dispatcher = RunDispatcher(sessionmaker, echo_agent)  # 不 start：run 停在排队，便于测取消
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _setup(api):
    uid = (await api.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]
    h = {"X-User-Id": uid}
    cid = (await api.post("/api/conversations", json={}, headers=h)).json()["id"]
    run = (await api.post(f"/api/conversations/{cid}/messages", json={"content": "q"}, headers=h)).json()
    return h, cid, run


async def test_cancel_endpoint_statuses(api, sessionmaker):
    h, cid, run = await _setup(api)
    r = await api.post(f"/api/runs/{run['id']}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await api.post(f"/api/runs/{run['id']}/cancel", headers=h)).status_code == 200  # 重复请求无害

    run2 = (await api.post(f"/api/conversations/{cid}/messages", json={"content": "q2"}, headers=h)).json()
    svc = RunService(sessionmaker)
    await svc.claim(uuid.UUID(run2["id"]))
    await svc.complete(uuid.UUID(run2["id"]), uuid.UUID(cid), "a")
    assert (await api.post(f"/api/runs/{run2['id']}/cancel", headers=h)).status_code == 409

    other = {"X-User-Id": (await api.post("/api/users", json={"name": f"o-{uuid.uuid4().hex[:6]}"})).json()["id"]}
    assert (await api.post(f"/api/runs/{run['id']}/cancel", headers=other)).status_code == 404


async def test_runs_listing_and_retry_endpoint(api):
    h, cid, run = await _setup(api)
    await api.post(f"/api/runs/{run['id']}/cancel", headers=h)
    r = await api.post(f"/api/runs/{run['id']}/retry", headers=h)
    assert r.status_code == 202
    runs = (await api.get(f"/api/conversations/{cid}/runs", headers=h)).json()
    assert [x["status"] for x in runs] == ["cancelled", "queued"]
    assert "error" not in runs[0]  # 原始报错不出现在接口里
    assert (await api.post(f"/api/runs/{runs[1]['id']}/retry", headers=h)).status_code == 409  # 排队中的不能重试


# ---------- 跨进程取消（decisions/0020） ----------


class _MemoryBus:
    """测试用的取消广播：直接转给“另一个进程”的 dispatcher。"""

    def __init__(self):
        self.subscribers = []
        self.published = []

    async def publish(self, run_id):
        self.published.append(run_id)
        for d in self.subscribers:
            d.on_cancel_message(run_id)


async def _long_running(sessionmaker, user):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def long_task(ctx):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    return run, long_task, started, cancelled


async def test_cancel_from_the_api_process_reaches_the_worker_via_the_bus(sessionmaker, user, redis):
    run, long_task, started, cancelled = await _long_running(sessionmaker, user)
    bus = _MemoryBus()
    worker = RunDispatcher(sessionmaker, long_task, sweep_interval=0.1, events=RunEventPublisher(redis),
                           heartbeat_seconds=30)  # 心跳很慢：证明是广播让它立刻停下的
    bus.subscribers.append(worker)
    api = RunDispatcher(sessionmaker, echo_agent, cancel_bus=bus)  # api 进程：不执行，只发取消
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert await api.cancel(run.id) == CancelOutcome.REQUESTED
        got = await _wait_status(sessionmaker, run.id, RunStatus.CANCELLED, timeout=3)
    finally:
        await worker.stop()
    assert bus.published == [run.id] and cancelled.is_set() and got.cancel_requested_at is not None
    assert await _events(redis, run.id) == [RUN_STARTED, RUN_CANCELLED]


async def test_cancel_still_arrives_when_the_broadcast_is_lost(sessionmaker, user):
    """广播丢了（Redis 抖动、worker 正在重连）：心跳时检查数据库里的取消请求，最多晚一个心跳间隔。"""
    run, long_task, started, cancelled = await _long_running(sessionmaker, user)
    worker = RunDispatcher(sessionmaker, long_task, sweep_interval=0.1, heartbeat_seconds=0.2)
    api = RunDispatcher(sessionmaker, echo_agent, cancel_bus=_MemoryBus())  # 广播发出去了，但没有任何订阅者收到
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert await api.cancel(run.id) == CancelOutcome.REQUESTED
        await _wait_status(sessionmaker, run.id, RunStatus.CANCELLED, timeout=3)
    finally:
        await worker.stop()
    assert cancelled.is_set()
