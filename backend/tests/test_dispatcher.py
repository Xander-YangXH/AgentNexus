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


async def test_running_run_without_lease_is_taken_over(sessionmaker, user, make_dispatcher):
    """升级前就在执行中、没有租约的 run（或者执行者已经死了）：租约视为过期，被接管并执行完（decisions/0020）。

    阶段 1 的做法是启动时把所有 running 标为失败——只在单进程时成立，多个 worker 时会误伤别人正在执行的 run。
    """
    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    assert await svc.claim(run.id)  # 模拟：库里留下 running，但没有租约

    d = make_dispatcher()
    await d.start()
    try:
        got = await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
        assert got.attempts == 1 and got.lease_version == 1
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


async def test_shutdown_releases_the_lease_and_another_worker_resumes(sessionmaker, user, make_dispatcher):
    """优雅关停：不再把在途 run 标为失败，而是放弃租约，别的 worker 立刻接手（attempt 2）。"""
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
    assert got.status == RunStatus.RUNNING and got.attempts == 1  # 没有被标失败

    seen = []

    async def record(ctx):
        seen.append(ctx.attempt)
        return "resumed"

    d2 = make_dispatcher(record)
    await d2.start()
    try:
        done = await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
    finally:
        await d2.stop()
    assert seen == [2] and done.attempts == 2 and done.lease_version == 2


async def test_expired_lease_is_taken_over_and_the_old_worker_is_fenced(sessionmaker, user, make_dispatcher):
    """旧 worker 卡住（不续约）→ 租约过期 → 新 worker 接管；旧 worker 恢复后既不能写回结果，也会被心跳发现并停止。"""
    release = asyncio.Event()
    old_cancelled = asyncio.Event()

    async def stuck(ctx):
        try:
            await release.wait()  # 卡住，直到测试放行
        except asyncio.CancelledError:
            old_cancelled.set()
            raise
        return "from the old worker"

    conv = await _conv(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    # 旧 worker：心跳间隔远大于租约，模拟“进程被暂停、没能续约”。并发设为 1：整个进程被暂停时所有执行槽都停了；
    # 如果有空闲的执行槽，它会自己把这个过期的 run 接管过去（第一版测试就是这样，版本号变成了 3）——这也是正确行为
    old = make_dispatcher(stuck, lease_seconds=0.5, heartbeat_seconds=1.5, owner="old", concurrency=1)
    await old.start()
    await _wait_status(sessionmaker, run.id, RunStatus.RUNNING)
    await asyncio.sleep(0.8)  # 租约过期
    new_running, finish_new = asyncio.Event(), asyncio.Event()

    async def new_handler(ctx):
        new_running.set()
        await finish_new.wait()  # 新 worker 保持执行中：只有这时，版本号才是挡住旧 worker 的唯一条件
        return "from the new worker"

    new = make_dispatcher(new_handler, owner="new")
    await new.start()
    try:
        await asyncio.wait_for(new_running.wait(), 3)
        # 旧 worker 心跳时发现租约丢了（状态仍是 running，只能靠版本号判断）：停止执行，什么都不写。
        # 第一版测试里新 worker 很快就执行完了，状态已不是 running，去掉版本校验也测不出来（变异检查发现）
        await asyncio.wait_for(old_cancelled.wait(), 3)
        # 旧 worker 绕过心跳直接写回：fencing 拒绝
        assert await svc.complete(run.id, conv.id, "stale", version=1) is None
        assert await svc.fail(run.id, "stale", version=1) is False
        finish_new.set()
        done = await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
        assert (done.lease_owner, done.lease_version, done.attempts) == ("new", 2, 2)
    finally:
        release.set()
        finish_new.set()
        await old.stop()
        await new.stop()
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
    assert [m.content for m in msgs] == ["q", "from the new worker"]


async def test_gives_up_after_max_attempts(sessionmaker, user, make_dispatcher):
    from sqlalchemy import update

    from agentnexus.storage.postgres import AgentRun

    conv = await _conv(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    async with sessionmaker() as session, session.begin():  # 模拟：已经被打断过 3 次
        await session.execute(update(AgentRun).where(AgentRun.id == run.id).values(status=RunStatus.RUNNING, attempts=3))
    called = []
    d = make_dispatcher(lambda ctx: called.append(1), max_attempts=3)
    await d.start()
    try:
        got = await _wait_status(sessionmaker, run.id, RunStatus.FAILED)
    finally:
        await d.stop()
    assert (got.error_code, called) == ("interrupted", []) and "gave up" in got.error


async def test_many_workers_execute_each_run_exactly_once(sessionmaker, user, make_dispatcher):
    calls = []

    async def counting(ctx):
        calls.append(ctx.run_id)
        await asyncio.sleep(0.05)
        return "ok"

    svc = RunService(sessionmaker)
    runs = []
    for _ in range(12):
        conv = await _conv(sessionmaker, user)
        runs.append(await svc.submit(user.id, conv.id, "q"))
    workers = [make_dispatcher(counting, concurrency=3, owner=f"w{i}") for i in range(3)]
    for w in workers:
        await w.start()
    try:
        for r in runs:
            await _wait_status(sessionmaker, r.id, RunStatus.SUCCEEDED)
    finally:
        for w in workers:
            await w.stop()
    assert sorted(calls) == sorted(r.id for r in runs)  # 每个 run 恰好执行一次
    owners = {(await _run(sessionmaker, r.id)).lease_owner for r in runs}
    assert len(owners) > 1  # 确实被多个 worker 分担了
