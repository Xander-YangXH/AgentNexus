"""checkpoint 清理（decisions/0010）。"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.models.fake import FakeEchoChatModel
from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.repositories.runs import AgentRunRepository, purge_candidates_stmt
from agentnexus.runtime.janitor import CheckpointJanitor
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import AgentRun

NOW = datetime(2030, 1, 1, tzinfo=UTC)
RETENTION = timedelta(hours=24)


async def _make_runs(sessionmaker, user, checkpointer, n, *, finish=True):
    """建 n 个对话各一个 run，用真实的聊天图执行一次以产生 checkpoint。"""
    svc, agent, ids = RunService(sessionmaker), ChatbotAgent(ChatbotContext(model=FakeEchoChatModel()), checkpointer), []
    for _ in range(n):
        async with sessionmaker() as session, session.begin():
            conv = await ConversationRepository(session).create(user.id)
        run = await svc.submit(user.id, conv.id, "q")
        await svc.claim(run.id)
        await agent.run(RunContext(run_id=run.id, conversation_id=conv.id, history=[ChatMessage("user", "q")]))
        if finish:
            await svc.complete(run.id, conv.id, "a")
        ids.append(run.id)
    return ids


async def _set_finished_at(sessionmaker, run_ids, when):
    async with sessionmaker() as session, session.begin():
        await session.execute(update(AgentRun).where(AgentRun.id.in_(run_ids)).values(finished_at=when))


async def _has_checkpoint(checkpointer, run_id) -> bool:
    return await checkpointer.aget_tuple({"configurable": {"thread_id": str(run_id)}}) is not None


async def _purged_at(sessionmaker, run_id):
    async with sessionmaker() as session:
        return (await AgentRunRepository(session).get(run_id)).checkpoint_purged_at


async def test_purges_only_finished_runs_past_retention(sessionmaker, user, checkpointer):
    old = await _make_runs(sessionmaker, user, checkpointer, 2)
    recent = await _make_runs(sessionmaker, user, checkpointer, 1)
    running = await _make_runs(sessionmaker, user, checkpointer, 1, finish=False)
    await _set_finished_at(sessionmaker, old, NOW - timedelta(hours=25))
    await _set_finished_at(sessionmaker, recent, NOW - timedelta(hours=1))

    janitor = CheckpointJanitor(sessionmaker, checkpointer, retention=RETENTION)
    assert await janitor.run_once(now=NOW) == 2
    for rid in old:
        assert not await _has_checkpoint(checkpointer, rid)  # checkpoint 真的没了
        assert await _purged_at(sessionmaker, rid) is not None  # 并且记了账
    for rid in recent + running:
        assert await _has_checkpoint(checkpointer, rid)
        assert await _purged_at(sessionmaker, rid) is None
    assert await janitor.run_once(now=NOW) == 0  # 第二轮无事可做


async def test_a_failing_delete_does_not_block_others_and_is_retried(sessionmaker, user, checkpointer):
    ids = await _make_runs(sessionmaker, user, checkpointer, 3)
    await _set_finished_at(sessionmaker, ids, NOW - timedelta(days=2))
    bad = str(ids[1])

    class Flaky:
        def __init__(self, inner):
            self.inner, self.fail = inner, True

        async def adelete_thread(self, thread_id):
            if thread_id == bad and self.fail:
                raise RuntimeError("db hiccup")
            await self.inner.adelete_thread(thread_id)

    flaky = Flaky(checkpointer)
    janitor = CheckpointJanitor(sessionmaker, flaky, retention=RETENTION)
    assert await janitor.run_once(now=NOW) == 2
    assert await _purged_at(sessionmaker, ids[1]) is None  # 失败的没有记账
    assert await _has_checkpoint(checkpointer, ids[1])
    flaky.fail = False
    assert await janitor.run_once(now=NOW) == 1  # 下一轮重试成功
    assert not await _has_checkpoint(checkpointer, ids[1])


async def test_loop_drains_a_backlog_larger_than_one_batch(sessionmaker, user, checkpointer):
    ids = await _make_runs(sessionmaker, user, checkpointer, 5)
    await _set_finished_at(sessionmaker, ids, datetime.now(UTC) - timedelta(days=2))
    janitor = CheckpointJanitor(sessionmaker, checkpointer, retention=RETENTION, batch_size=2, interval_seconds=3600)
    janitor.start()
    try:
        for _ in range(100):  # 间隔是 1 小时：只有“一批满了就继续下一批”才能在这里清完
            if all([await _purged_at(sessionmaker, r) for r in ids]):
                break
            await asyncio.sleep(0.02)
    finally:
        await janitor.stop()
    assert all([await _purged_at(sessionmaker, r) for r in ids])


async def test_candidate_query_can_use_the_partial_index(sessionmaker):
    """部分索引只有在查询条件“蕴含”索引条件时才会被使用；条件写错不会报错，只是悄悄变成全表扫描。"""
    stmt = purge_candidates_stmt(NOW, 100)
    async with sessionmaker() as session:
        conn = await session.connection()
        compiled = stmt.compile(dialect=conn.dialect)
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")  # 表太小时规划器总会选全表扫描，关掉才能看出“能否用索引”
        plan = "\n".join(r[0] for r in (await conn.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params)).all())
    assert "ix_agent_runs_checkpoint_purge_candidates" in plan, plan
