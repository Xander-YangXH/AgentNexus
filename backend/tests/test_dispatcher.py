import asyncio

import pytest

from agentnexus.agents.echo import echo_agent
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus


async def _conv(sessionmaker, user):
    async with sessionmaker() as session, session.begin():
        return await ConversationRepository(session).create(user.id)


async def _run(sessionmaker, run_id):
    async with sessionmaker() as session:
        return await AgentRunRepository(session).get(run_id)


async def _wait_status(sessionmaker, run_id, *statuses, timeout=5.0):
    async def poll():
        while True:
            run = await _run(sessionmaker, run_id)
            if run.status in statuses:
                return run
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)


@pytest.fixture
def make_dispatcher(sessionmaker):
    def factory(handler=echo_agent, **kw):
        kw.setdefault("sweep_interval", 0.1)
        return RunDispatcher(sessionmaker, handler, **kw)

    return factory


async def test_enqueue_runs_echo_agent_end_to_end(sessionmaker, user, make_dispatcher):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    d = make_dispatcher()
    await d.start()
    try:
        run = await svc.submit(user.id, conv.id, "hello")
        d.enqueue(run.id)
        done = await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
        async with sessionmaker() as session:
            msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [(m.role, m.content) for m in msgs] == [("user", "hello"), ("assistant", "echo: hello")]
        assert done.output_message_id == msgs[1].id
    finally:
        await d.stop()


async def test_sweep_picks_up_queued_run_that_was_never_enqueued(sessionmaker, user, make_dispatcher):
    """提交成功但投递丢了（进程崩溃、队列重启）：靠扫描 queued 补投。"""
    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "lost?")  # 故意不 enqueue
    d = make_dispatcher()
    await d.start()
    try:
        await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
    finally:
        await d.stop()


async def test_startup_recovery_fails_orphaned_running_and_frees_conversation(
    sessionmaker, user, make_dispatcher
):
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    assert await svc.claim(run.id)  # 模拟上一个进程执行到一半被杀：库里留下 running

    d = make_dispatcher()
    await d.start()
    try:
        got = await _run(sessionmaker, run.id)
        assert got.status == RunStatus.FAILED
        assert "restarted" in got.error
        await svc.submit(user.id, conv.id, "again")  # 对话没有被卡死
    finally:
        await d.stop()


async def test_handler_exception_marks_failed_with_error(sessionmaker, user, make_dispatcher):
    async def boom(ctx):
        raise ValueError("bad prompt")

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = make_dispatcher(boom)
    await d.start()
    try:
        d.enqueue(run.id)
        got = await _wait_status(sessionmaker, run.id, RunStatus.FAILED)
        assert got.error == "ValueError: bad prompt"
    finally:
        await d.stop()


async def test_timeout_marks_failed(sessionmaker, user, make_dispatcher):
    async def slow(ctx):
        await asyncio.sleep(10)

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = make_dispatcher(slow, run_timeout=0.2)
    await d.start()
    try:
        got = await _wait_status(sessionmaker, run.id, RunStatus.FAILED)
        assert got.error.startswith("timeout")
    finally:
        await d.stop()


async def test_duplicate_enqueue_executes_once(sessionmaker, user, make_dispatcher):
    calls = []

    async def counting(ctx):
        calls.append(ctx.run_id)
        await asyncio.sleep(0.1)
        return "ok"

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = make_dispatcher(counting)
    await d.start()
    try:
        assert d.enqueue(run.id) in (True, False)  # 可能已被启动时的扫描入队
        d.enqueue(run.id)
        await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
        await asyncio.sleep(0.4)  # 多过几轮扫描，确认不会被重复执行
        assert calls == [run.id]
    finally:
        await d.stop()


async def test_concurrency_limit_is_respected_and_runs_overlap(sessionmaker, user, make_dispatcher):
    running = peak = 0

    async def tracked(ctx):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.15)
        running -= 1
        return "ok"

    svc = RunService(sessionmaker)
    runs = []
    for _ in range(5):  # 5 个不同对话，各一个 run
        conv = await _conv(sessionmaker, user)
        runs.append(await svc.submit(user.id, conv.id, "q"))

    d = make_dispatcher(tracked, concurrency=2)
    await d.start()
    try:
        for r in runs:
            await _wait_status(sessionmaker, r.id, RunStatus.SUCCEEDED)
        assert peak == 2  # 既发生了并行，又没有超过上限
    finally:
        await d.stop()


async def test_shutdown_marks_in_flight_run_failed(sessionmaker, user, make_dispatcher):
    started = asyncio.Event()

    async def hang(ctx):
        started.set()
        await asyncio.sleep(60)

    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    d = make_dispatcher(hang)
    await d.start()
    await asyncio.wait_for(started.wait(), 3)
    await d.stop()  # 关停时 run 正在执行

    got = await _run(sessionmaker, run.id)
    assert (got.status, got.error) == (RunStatus.FAILED, "interrupted: shutdown")
    await svc.submit(user.id, conv.id, "after")  # 对话仍可继续使用
