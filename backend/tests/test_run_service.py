import asyncio
import uuid

import pytest

from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.services.errors import ConversationNotFound, RunConflict
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import MessageRole, RunStatus, User


async def _new_conversation(sessionmaker, user):
    async with sessionmaker() as session, session.begin():
        return await ConversationRepository(session).create(user.id)


async def test_submit_writes_message_and_queued_run_atomically(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "你好")

    assert run.status == RunStatus.QUEUED
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [(m.role, m.content) for m in msgs] == [(MessageRole.USER, "你好")]
        assert run.input_message_id == msgs[0].id
        queued = await AgentRunRepository(session).list_queued()
        assert [r.id for r in queued] == [run.id]


async def test_submit_rejects_other_users_conversation(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    with pytest.raises(ConversationNotFound):
        await RunService(sessionmaker).submit(uuid.uuid4(), conv.id, "x")
    with pytest.raises(ConversationNotFound):
        await RunService(sessionmaker).submit(user.id, uuid.uuid4(), "x")


async def test_failed_submit_leaves_no_orphan_message(sessionmaker, user):
    """第二次提交因"已有活跃 run"失败，整个事务回滚，用户消息也不能留下。"""
    conv = await _new_conversation(sessionmaker, user)
    svc = RunService(sessionmaker)
    await svc.submit(user.id, conv.id, "第一条")
    with pytest.raises(RunConflict):
        await svc.submit(user.id, conv.id, "第二条")
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [m.content for m in msgs] == ["第一条"]


async def test_new_run_allowed_after_previous_finished(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    svc = RunService(sessionmaker)
    run1 = await svc.submit(user.id, conv.id, "q1")
    assert await svc.claim(run1.id)
    assert await svc.complete(run1.id, conv.id, "a1")
    run2 = await svc.submit(user.id, conv.id, "q2")
    assert run2.id != run1.id

    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [(m.role, m.content) for m in msgs] == [
            ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
        ]


async def test_claim_is_exclusive_under_concurrency(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    results = await asyncio.gather(*(svc.claim(run.id) for _ in range(8)))
    assert results.count(True) == 1


async def test_complete_only_from_running_and_leaves_no_orphan(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    # 还是 queued，没被 claim：不允许直接 complete，助手消息也不能落库
    assert not await svc.complete(run.id, conv.id, "a")
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [m.role for m in msgs] == ["user"]
        assert (await AgentRunRepository(session).get(run.id)).status == RunStatus.QUEUED


async def test_fail_records_error_and_frees_conversation(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    svc = RunService(sessionmaker)
    run = await svc.submit(user.id, conv.id, "q")
    await svc.claim(run.id)
    assert await svc.fail(run.id, "LLM timeout")
    assert not await svc.fail(run.id, "again")  # 终态不可再改
    async with sessionmaker() as session:
        got = await AgentRunRepository(session).get(run.id)
        assert (got.status, got.error) == (RunStatus.FAILED, "LLM timeout")
        assert got.finished_at is not None
    await svc.submit(user.id, conv.id, "retry")  # 失败后可以重新提交


async def test_messages_ordered_by_seq_within_one_transaction(sessionmaker, user):
    conv = await _new_conversation(sessionmaker, user)
    async with sessionmaker() as session, session.begin():
        repo = MessageRepository(session)
        for i in range(5):
            await repo.add(conv.id, MessageRole.USER, f"m{i}")  # 同一事务，created_at 相同
    async with sessionmaker() as session:
        msgs = await MessageRepository(session).list_by_conversation(conv.id)
        assert [m.content for m in msgs] == [f"m{i}" for i in range(5)]
