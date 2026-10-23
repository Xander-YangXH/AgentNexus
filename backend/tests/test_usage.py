"""用量采集与聚合（阶段 5，decisions/0023）。"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.agents.buildin.research import ResearchAgent, ResearchContext
from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.security.principal import Principal
from agentnexus.services.runs import RunService
from agentnexus.services.usage import UsageRecorder, UsageService, usage_insert
from agentnexus.storage.postgres import Department, UsageEvent, UsageKind, User
from tests.test_research import RecordingPlanner


class UsageModel(BaseChatModel):
    """每次调用回复固定文本，并带上用量（真实供应商的 usage_metadata 形状）。"""

    @property
    def _llm_type(self):
        return "usage-fake"

    def bind_tools(self, tools, **kw):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        msg = AIMessage(content="好的", usage_metadata={"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
                                                         "input_token_details": {"cache_read": 400}})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _generate(self, *a, **k):
        raise NotImplementedError


async def _events(sessionmaker, user_id):
    async with sessionmaker() as s:
        return list((await s.scalars(select(UsageEvent).where(UsageEvent.user_id == user_id).order_by(UsageEvent.id))).all())


async def _dept_user(sessionmaker, tag="u"):
    async with sessionmaker() as s, s.begin():
        d = Department(name=f"部门-{uuid.uuid4().hex[:6]}")
        s.add(d)
        await s.flush()
        u = User(name=f"{tag}-{uuid.uuid4().hex[:6]}", department_id=d.id)
        s.add(u)
        await s.flush()
        return u


async def test_chat_run_records_every_model_call_with_department(sessionmaker):
    user = await _dept_user(sessionmaker)
    agent = ChatbotAgent(ChatbotContext(model=UsageModel(), usage=UsageRecorder(sessionmaker, model="m1")))
    ctx = RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", "hi")], user_id=user.id)
    await agent.run(ctx)
    [e] = await _events(sessionmaker, user.id)
    assert (e.kind, e.source, e.run_id, e.model) == ("llm", "chat", ctx.run_id, "m1")
    assert (e.input_tokens, e.cached_tokens, e.output_tokens, e.department_id) == (1000, 400, 50, user.department_id)


async def test_research_records_subagent_and_writer_calls(sessionmaker):
    """实验 4 在实现里成立：并行子 Agent 和汇总的调用都被回调记下（计划用的是不调模型的假规划器）。"""
    user = await _dept_user(sessionmaker)
    agent = ResearchAgent(ResearchContext(model=UsageModel(), planner=RecordingPlanner(),
                                          usage=UsageRecorder(sessionmaker)), checkpointer=InMemorySaver())
    ctx = RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", "甲、乙、丙")],
                     user_id=user.id)
    await agent.run(ctx)
    await agent.run(dataclasses.replace(ctx, approval={"approved": True}))
    events = await _events(sessionmaker, user.id)
    assert len(events) == 4 and {e.source for e in events} == {"research"}  # 3 个子 Agent + 1 次汇总


async def test_calls_before_a_crash_are_still_recorded(sessionmaker):
    """4.3 的局限：用量记在图状态里，崩溃那次执行的在途调用会丢。回调在调用结束时就写库，与图是否提交无关。"""
    user = await _dept_user(sessionmaker)

    class Crash(BaseException):
        pass

    async def crashing_search(user_id, kb_ids, query):
        raise Crash()

    calls = []

    class ToolFirst(UsageModel):
        async def _agenerate(self, messages, *a, **kw):
            calls.append(1)
            r = await super()._agenerate(messages, *a, **kw)
            r.generations[0].message.tool_calls = [{"name": "knowledge_search", "args": {"query": "x"}, "id": "t1", "type": "tool_call"}]
            return r

    agent = ChatbotAgent(ChatbotContext(model=ToolFirst(), knowledge_search=crashing_search,
                                        usage=UsageRecorder(sessionmaker)))
    ctx = RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", "hi")],
                     user_id=user.id, knowledge_base_ids=(uuid.uuid4(),))
    with pytest.raises(Crash):
        await agent.run(ctx)  # 模型调用完成后，执行工具时“进程死掉”
    assert len(await _events(sessionmaker, user.id)) == len(calls) == 1


async def test_submit_records_a_request_in_the_same_transaction(sessionmaker):
    user = await _dept_user(sessionmaker)
    async with sessionmaker() as s, s.begin():
        conv = await ConversationRepository(s).create(user.id, None, "research")
    run = await RunService(sessionmaker).submit(user.id, conv.id, "调研")
    [e] = await _events(sessionmaker, user.id)
    assert (e.kind, e.source, e.run_id) == ("run", "research", run.id)


async def test_recording_failure_never_breaks_the_caller(sessionmaker, caplog):
    await UsageRecorder(sessionmaker).record(uuid.uuid4(), UsageKind.LLM, "chat")  # 用户不存在：外键失败
    assert "recording usage failed" in caplog.text


async def test_summary_groups_by_day_and_estimates_cost(sessionmaker):
    user = await _dept_user(sessionmaker)
    now = datetime.now(UTC)
    async with sessionmaker() as s, s.begin():
        for days_ago, kind, tokens in [(0, "llm", (1000, 400, 100)), (0, "llm", (1000, 0, 100)), (0, "run", (0, 0, 0)),
                                       (1, "search", (0, 0, 0)), (40, "llm", (999, 0, 999))]:
            stmt = usage_insert(user.id, kind, "chat", input_tokens=tokens[0], cached_tokens=tokens[1], output_tokens=tokens[2])
            await s.execute(stmt.values(created_at=now - timedelta(days=days_ago)))
    svc = UsageService(sessionmaker, price_input=0.3, price_cached=0.006, price_output=1.2)
    p = Principal(user.id, user.name, "member", user.department_id)
    got = await svc.summary(p, days=30, now=now)
    t = got["totals"]
    assert (t["runs"], t["llm_calls"], t["searches"], t["input_tokens"], t["cached_tokens"], t["output_tokens"]) == (1, 2, 1, 2000, 400, 200)
    assert t["cost_usd"] == round((1600 * 0.3 + 400 * 0.006 + 200 * 1.2) / 1e6, 6)  # 30 天之外的那条不算
    assert [d["llm_calls"] for d in got["series"]] == [0, 2] and len(got["series"]) == 2
    with pytest.raises(PermissionError):
        await svc.summary(p, scope="all")
