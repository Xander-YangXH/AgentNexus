"""深度调研（阶段 4.3，decisions/0022）：计划审批、并行子 Agent、崩溃续做、报告产物，以及 run 的等待审批状态。

模型用规则化的假模型，不调用真实模型。
"""

import asyncio
import dataclasses
import uuid

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

from agentnexus.agents.buildin.research import ResearchAgent, ResearchContext
from agentnexus.agents.buildin.research.planner import FakePlanner, PlanResult
from agentnexus.agents.buildin.research.prompt import SUBAGENT_PROMPT, WRITER_PROMPT
from agentnexus.agents.tools import AgentTool, StaticTools
from agentnexus.agents.types import AgentReply, ApprovalRequired, ChatMessage, RunContext
from agentnexus.events import RESEARCH_SUBTASK, RUN_AWAITING_APPROVAL, RUN_COMPLETED, RUN_STARTED, TOKEN, TOOL_STARTED
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader
from agentnexus.repositories.conversations import ConversationRepository, MessageRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.runtime.janitor import CheckpointJanitor
from agentnexus.services.artifacts import ArtifactService
from agentnexus.services.errors import RunConflict
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.services.runs import RunService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.postgres import RunStatus


class Crash(BaseException):
    """模拟 worker 进程在子任务执行中途死掉（不是普通异常：子 Agent 的 except Exception 接不住）。"""


class FakeResearchModel(BaseChatModel):
    """按系统提示区分角色：子 Agent 返回“<子任务>的结论”（可配置先调一次工具）；汇总返回一份报告。"""

    tool_first: bool = False
    _calls: list = PrivateAttr(default_factory=list)
    _bound: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "fake-research"

    def bind_tools(self, tools, **kwargs):
        bound = self.model_copy()
        bound._calls = self._calls
        bound._bound = [t["function"]["name"] for t in tools]
        return bound

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        system, human = messages[0].content, messages[1].content
        if system == WRITER_PROMPT:
            self._calls.append(("write", None))
            n = human.count("## 子任务")
            msg = AIMessage(content=f"# 调研报告\n\n汇总了 {n} 个子任务。")
        else:
            assert system == SUBAGENT_PROMPT
            title = human.split("你的子任务：")[1].split("\n")[0]
            self._calls.append(("sub", title, list(self._bound)))
            if self.tool_first and len(messages) == 2 and self._bound:
                msg = AIMessage(content="", tool_calls=[{"name": self._bound[0], "args": {"query": title, "url": f"https://x/{title}"},
                                                         "id": f"t-{title}", "type": "tool_call"}])
            else:
                msg = AIMessage(content=f"{title}的结论")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _generate(self, *a, **kw):
        raise NotImplementedError


class RecordingPlanner(FakePlanner):
    def __init__(self):
        super().__init__(max_subtasks=5)
        self.calls = []

    async def __call__(self, request, feedback, previous):
        self.calls.append((request, feedback, previous))
        return await super().__call__(request, feedback, previous)


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, type_, data=None):
        self.items.append((type_, data or {}))

    def of(self, type_):
        return [d for t, d in self.items if t == type_]


def _agent(model=None, planner=None, saved=None, **kw):
    saved = saved if saved is not None else []

    async def save(filename, content_type, data):
        saved.append((filename, data.decode()))
        return {"id": str(uuid.uuid4()), "filename": filename, "content_type": content_type, "size": len(data),
                "blob_key": "artifacts/x"}

    ctx = ResearchContext(model=model or FakeResearchModel(), planner=planner or RecordingPlanner(),
                          save_artifact=save, **kw)
    return ResearchAgent(ctx, checkpointer=InMemorySaver()), ctx


def _ctx(request="调研 甲、乙、丙", **kw):
    return RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", request)],
                      events=Events(), user_id=uuid.uuid4(), **kw)


# ---------- Agent 本身 ----------

async def test_plan_is_approved_before_any_subagent_runs_and_planner_is_called_once():
    model, planner, saved = FakeResearchModel(), RecordingPlanner(), []
    agent, _ = _agent(model, planner, saved)
    ctx = _ctx()
    first = await agent.run(ctx)
    assert isinstance(first, ApprovalRequired)
    assert [s["title"] for s in first.request["plan"]] == ["调研 甲", "乙", "丙"]
    assert first.request["estimate"]["subtasks"] == 3
    assert model._calls == []  # 批准之前没有任何子 Agent 调用模型

    done = await agent.run(dataclasses.replace(ctx, approval={"approved": True}, events=Events()))
    assert isinstance(done, AgentReply) and done.content.startswith("# 调研报告") and "3 个子任务" in done.content
    assert len(planner.calls) == 1  # 恢复时没有重新制定计划：批准的就是执行的
    assert sorted(c[1] for c in model._calls if c[0] == "sub") == ["丙", "乙", "调研 甲"]
    assert done.attachments[0]["filename"].startswith("调研报告-") and saved[0][1] == done.content


async def test_rejecting_with_feedback_replans_with_the_previous_plan():
    planner = RecordingPlanner()
    agent, _ = _agent(planner=planner)
    ctx = _ctx()
    await agent.run(ctx)
    again = await agent.run(dataclasses.replace(ctx, approval={"approved": False, "feedback": "去掉丙"}))
    assert isinstance(again, ApprovalRequired) and len(again.request["plan"]) == 2
    request, feedback, previous = planner.calls[1]
    assert feedback == "去掉丙" and [s["title"] for s in previous] == ["调研 甲", "乙", "丙"]


async def test_progress_events_and_only_the_report_is_streamed():
    agent, _ = _agent()
    ctx = _ctx()
    await agent.run(ctx)
    events = Events()
    await agent.run(dataclasses.replace(ctx, approval={"approved": True}, events=events))
    sub = events.of(RESEARCH_SUBTASK)
    assert sorted((d["title"], d["status"]) for d in sub) == sorted(
        [(t, s) for t in ("调研 甲", "乙", "丙") for s in ("started", "finished")])
    assert all(d["total"] == 3 for d in sub)
    assert "".join(d["text"] for d in events.of(TOKEN)) == "# 调研报告\n\n汇总了 3 个子任务。"  # 子 Agent 的结论不流给用户


async def test_crash_mid_fanout_only_redoes_unfinished_subtasks():
    """实验 1 的结论在实现里成立：子任务“丙”执行中进程崩溃，接管后只重做“丙”。

    崩溃放在检索工具里：放在模型里不行——LangChain 的 agenerate 会把模型抛出的 BaseException 变成 AttributeError。
    """
    crashed = set()

    async def search(user_id, kb_ids, query):
        if query == "丙" and "丙" not in crashed:
            await asyncio.sleep(0.2)  # 让别的子任务先完成
            crashed.add("丙")
            raise Crash()
        return []

    model = FakeResearchModel(tool_first=True)
    agent, _ = _agent(model, knowledge_search=search)
    ctx = _ctx(knowledge_base_ids=(uuid.uuid4(),))
    await agent.run(ctx)
    with pytest.raises(Crash):
        await agent.run(dataclasses.replace(ctx, approval={"approved": True}))
    before = [c[1] for c in model._calls if c[0] == "sub"]
    assert sorted(before) == ["丙", "乙", "乙", "调研 甲", "调研 甲"]  # 甲、乙完成（各两次调用），丙在工具里崩溃
    done = await agent.run(dataclasses.replace(ctx, approval={"approved": True}, attempt=2))
    after = [c[1] for c in model._calls if c[0] == "sub"][len(before):]
    assert after == ["丙", "丙"] and "3 个子任务" in done.content  # 丙从头做（调工具、再给结论），甲、乙不再调用模型


async def test_subagents_get_only_search_and_fetch_tools():
    fetched = []

    async def fetch(args, scope):
        fetched.append((args["url"], scope.workspace_id))
        return "网页内容"

    async def python_exec(args, scope):
        raise AssertionError("子 Agent 不应能执行代码")

    tools = StaticTools([AgentTool("fetch", "抓网页", {"type": "object", "properties": {}}, fetch),
                         AgentTool("python_exec", "执行代码", {"type": "object", "properties": {}}, python_exec)])
    model = FakeResearchModel(tool_first=True)
    agent, _ = _agent(model, tool_sources=[tools])
    ctx = _ctx("调研 甲")
    await agent.run(ctx)
    events = Events()
    await agent.run(dataclasses.replace(ctx, approval={"approved": True}, events=events))
    assert {tuple(c[2]) for c in model._calls if c[0] == "sub"} == {("fetch",)}
    assert fetched == [("https://x/调研 甲", str(ctx.conversation_id))]
    assert events.of(TOOL_STARTED)[0]["subtask"] == 0


async def test_failed_subtask_is_reported_not_fatal():
    class Flaky(FakeResearchModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if "你的子任务：乙" in messages[1].content:
                raise RuntimeError("llm down")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    agent, _ = _agent(Flaky())
    ctx = _ctx()
    await agent.run(ctx)
    events = Events()
    done = await agent.run(dataclasses.replace(ctx, approval={"approved": True}, events=events))
    assert "3 个子任务" in done.content
    assert [d["status"] for d in events.of(RESEARCH_SUBTASK) if d["title"] == "乙"] == ["started", "failed"]


# ---------- run 的等待审批状态（数据库 + 派发器 + 接口） ----------

async def _research_conv(sessionmaker, user):
    async with sessionmaker() as session, session.begin():
        return await ConversationRepository(session).create(user.id, None, "research")


async def _wait_status(sessionmaker, run_id, *statuses, timeout=5.0):
    async def poll():
        while True:
            async with sessionmaker() as session:
                r = await AgentRunRepository(session).get(run_id)
            if r.status in statuses:
                return r
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)


@pytest.fixture
def research_handler(tmp_path):
    def make(sessionmaker, checkpointer):
        artifacts = ArtifactService(sessionmaker, LocalBlobStore(tmp_path / "blobs"))
        agent = ResearchAgent(ResearchContext(model=FakeResearchModel(), planner=RecordingPlanner(),
                                              save_artifact=artifacts.save), checkpointer=checkpointer)
        return agent.run, artifacts
    return make


async def test_awaiting_approval_frees_the_worker_and_resumes_after_approval(
        sessionmaker, checkpointer, user, redis, research_handler):
    handler, artifacts = research_handler(sessionmaker, checkpointer)
    svc = RunService(sessionmaker)
    conv = await _research_conv(sessionmaker, user)
    run = await svc.submit(user.id, conv.id, "调研 甲、乙")
    d = RunDispatcher(sessionmaker, handler, concurrency=1, sweep_interval=0.1, events=RunEventPublisher(redis))
    await d.start()
    try:
        waiting = await _wait_status(sessionmaker, run.id, RunStatus.AWAITING_APPROVAL)
        assert waiting.approval_request["kind"] == "research_plan" and len(waiting.approval_request["plan"]) == 2
        assert (waiting.lease_owner, waiting.lease_expires_at, waiting.finished_at) == (None, None, None)
        # 等待期间仍是活跃 run：同一对话不能再发消息
        with pytest.raises(RunConflict):
            await svc.submit(user.id, conv.id, "再来一个")
        # 只有一个执行槽，也没有被等待中的 run 占着：别的对话照常执行
        other = await svc.submit(user.id, (await _research_conv(sessionmaker, user)).id, "调研 丁")
        assert (await _wait_status(sessionmaker, other.id, RunStatus.AWAITING_APPROVAL)).status

        await svc.respond(user.id, run.id, {"approved": True})
        done = await _wait_status(sessionmaker, run.id, RunStatus.SUCCEEDED)
    finally:
        await d.stop()
    assert done.attempts == 1  # 审批后计数清零，不消耗接管次数
    async with sessionmaker() as session:
        reply = (await MessageRepository(session).list_by_conversation(conv.id))[-1]
    assert reply.attachments[0]["filename"].startswith("调研报告-")
    artifact, data = await artifacts.get(user.id, uuid.UUID(reply.attachments[0]["id"]))
    assert data.decode() == reply.content and artifact.run_id == run.id
    types = [e.type for e in await RunEventReader(redis).read(run.id, "0-0", block_ms=None)]
    assert types[:2] == [RUN_STARTED, RUN_AWAITING_APPROVAL] and types[-1] == RUN_COMPLETED


async def test_cancel_and_expiry_of_waiting_runs(sessionmaker, checkpointer, user, research_handler):
    handler, _ = research_handler(sessionmaker, checkpointer)
    svc = RunService(sessionmaker)
    a, b = await _research_conv(sessionmaker, user), await _research_conv(sessionmaker, user)
    run_a, run_b = await svc.submit(user.id, a.id, "调研 甲"), await svc.submit(user.id, b.id, "调研 乙")
    d = RunDispatcher(sessionmaker, handler, sweep_interval=0.1)
    await d.start()
    try:
        await _wait_status(sessionmaker, run_a.id, RunStatus.AWAITING_APPROVAL)
        await _wait_status(sessionmaker, run_b.id, RunStatus.AWAITING_APPROVAL)
        assert (await d.cancel(run_a.id)).value == "cancelled"  # 等待中没有人在执行：直接取消
    finally:
        await d.stop()
    janitor = CheckpointJanitor(sessionmaker, checkpointer, retention=__import__("datetime").timedelta(hours=24),
                                approval_ttl=__import__("datetime").timedelta(seconds=0))
    await janitor.run_once()
    got = await _wait_status(sessionmaker, run_b.id, RunStatus.CANCELLED)
    assert got.error_code == "approval_expired"
    from agentnexus.services.errors import RunNotAwaitingApproval
    with pytest.raises(RunNotAwaitingApproval):
        await svc.respond(user.id, run_b.id, {"approved": True})


async def test_sse_pauses_at_approval_and_skips_it_after_approval(sessionmaker, user, redis):
    svc = RunService(sessionmaker)
    conv = await _research_conv(sessionmaker, user)
    run = await svc.submit(user.id, conv.id, "调研")
    pub = RunEventPublisher(redis)
    async with sessionmaker() as session, session.begin():
        await AgentRunRepository(session).claim(run.id)
        await AgentRunRepository(session).await_approval(run.id, {"plan": []}, version=0)
    await pub.publish(run.id, RUN_STARTED, {})
    await pub.publish(run.id, RUN_AWAITING_APPROVAL, {"request": {"plan": []}})
    streamer = RunEventStreamer(sessionmaker, RunEventReader(redis), poll_interval=0.2)
    got = [e.type async for e in streamer.stream(run.id)]
    assert got == [RUN_STARTED, RUN_AWAITING_APPROVAL]  # 暂停：结束响应，不挂着连接

    await svc.respond(user.id, run.id, {"approved": True})
    await pub.publish(run.id, TOKEN, {"text": "继续"})
    async with sessionmaker() as session, session.begin():
        await AgentRunRepository(session).claim(run.id)
        await AgentRunRepository(session).finish(run.id, RunStatus.SUCCEEDED)
    await pub.publish(run.id, RUN_COMPLETED, {})
    replay = [e.type async for e in streamer.stream(run.id)]  # 刷新后从头回放：旧的审批事件被跳过，不会提前结束
    assert replay == [RUN_STARTED, TOKEN, RUN_COMPLETED]


async def test_approval_and_download_api(sessionmaker, user, tmp_path):
    from server.deps import get_artifact_service, get_dispatcher, get_sessionmaker
    from server.main import create_app

    app = create_app(with_lifespan=False)
    artifacts = ArtifactService(sessionmaker, LocalBlobStore(tmp_path / "b"))
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: RunDispatcher(sessionmaker, None)
    app.dependency_overrides[get_artifact_service] = lambda: artifacts
    h = {"X-User-Id": str(user.id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        conv = (await c.post("/api/conversations", json={"agent": "research"}, headers=h)).json()
        assert conv["agent"] == "research"
        assert (await c.post("/api/conversations", json={"agent": "bogus"}, headers=h)).status_code == 422
        run = (await c.post(f"/api/conversations/{conv['id']}/messages", json={"content": "调研"}, headers=h)).json()
        assert (await c.post(f"/api/runs/{run['id']}/approval", json={"approved": True}, headers=h)).status_code == 409
        async with sessionmaker() as session, session.begin():
            await AgentRunRepository(session).claim(uuid.UUID(run["id"]))
            await AgentRunRepository(session).await_approval(uuid.UUID(run["id"]), {"plan": [{"title": "甲"}]}, version=0)
        active = (await c.get(f"/api/conversations/{conv['id']}/active-run", headers=h)).json()
        assert active["status"] == "awaiting_approval" and active["approval_request"]["plan"][0]["title"] == "甲"
        assert (await c.post(f"/api/runs/{run['id']}/approval", json={"approved": False}, headers=h)).status_code == 422
        other = (await c.post("/api/users", json={"name": f"o-{uuid.uuid4().hex[:6]}"})).json()
        assert (await c.post(f"/api/runs/{run['id']}/approval", json={"approved": True},
                             headers={"X-User-Id": other["id"]})).status_code == 404
        r = await c.post(f"/api/runs/{run['id']}/approval", json={"approved": False, "feedback": "少一点"}, headers=h)
        assert r.status_code == 202 and r.json()["status"] == "queued"

        # 下载：只有自己能下；文件名是中文；作为附件、禁止类型嗅探
        att = await artifacts.save("调研报告.md", "text/markdown; charset=utf-8", "# 报告".encode())
        await RunService(sessionmaker).claim(uuid.UUID(run["id"]))
        await RunService(sessionmaker).complete(uuid.UUID(run["id"]), uuid.UUID(conv["id"]), "# 报告", attachments=[att])
        msgs = (await c.get(f"/api/conversations/{conv['id']}/messages", headers=h)).json()
        assert msgs[-1]["attachments"] == [{"id": att["id"], "filename": "调研报告.md",
                                            "content_type": "text/markdown; charset=utf-8", "size": len("# 报告".encode())}]
        got = await c.get(f"/api/artifacts/{att['id']}/download", headers=h)
        assert got.status_code == 200 and got.text == "# 报告"
        assert got.headers["content-disposition"].startswith("attachment;") and "filename*=UTF-8''" in got.headers["content-disposition"]
        assert got.headers["x-content-type-options"] == "nosniff"
        assert (await c.get(f"/api/artifacts/{att['id']}/download", headers={"X-User-Id": other["id"]})).status_code == 404
        assert (await c.get(f"/api/artifacts/{uuid.uuid4()}/download", headers=h)).status_code == 404
