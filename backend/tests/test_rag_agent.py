"""知识库问答（阶段 2.4，decisions/0018）：knowledge_search 工具、引用、对话关联知识库、端到端落库。"""

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphRecursionError
from pydantic import PrivateAttr

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.agents.buildin.chatbot.graph import ROUND_LIMIT_REACHED
from agentnexus.agents.buildin.chatbot.tools import NO_RESULT, format_for_model, mark_cited
from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.events import RUN_COMPLETED, TOKEN, TOOL_FINISHED, TOOL_STARTED
from agentnexus.knowledge.hits import SearchHit
from agentnexus.models.fake import FakeEchoChatModel
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.storage.blobs import LocalBlobStore
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import FakeEmbedder


def _hit(n, filename="退货.txt", text=None, page=None):
    return SearchHit(
        chunk_id=uuid.UUID(int=n), document_id=uuid.UUID(int=1000 + n), knowledge_base_id=uuid.UUID(int=9),
        filename=filename, seq=n, page=page, text=text or f"片段{n}的内容", vector_score=0.8, rerank_score=1.0,
    )


class RecordingSearch:
    def __init__(self, results=None, fail=False):
        self.calls = []
        self.results = results  # 每次调用依次返回的结果；None 表示总是返回两个片段
        self.fail = fail

    async def __call__(self, user_id, kb_ids, query):
        self.calls.append((user_id, list(kb_ids), query))
        if self.fail:
            raise ConnectionError("qdrant down")
        if self.results is None:
            return [_hit(1), _hit(2, filename="手册.pdf", page=3)]
        return self.results[len(self.calls) - 1]


class ScriptedModel(BaseChatModel):
    """按剧本依次返回消息；记录每次收到的消息与是否绑定了工具。"""

    script: list
    _calls: list = PrivateAttr(default_factory=list)
    _tools_bound: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self._tools_bound = True
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append((list(messages), self._tools_bound))
        step = self.script[min(len(self._calls) - 1, len(self.script) - 1)]
        # 每次返回副本且不带 id：同一个消息对象第一次返回后会被分配 id，再返回时 LangGraph 的 add_messages
        # 按 id 原地替换而不是追加（第一版测试踩到：“死循环”的模型被当成只说了一句，图提前结束）
        return ChatResult(generations=[ChatGeneration(message=step.model_copy(update={"id": None}))])


def _call(query, i=0, **extra):
    return AIMessage(content="", tool_calls=[{"name": "knowledge_search", "args": {"query": query, **extra},
                                              "id": f"c{i}", "type": "tool_call"}])


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, type, data):
        self.items.append((type, data))

    def of(self, type):
        return [d for t, d in self.items if t == type]


USER, KB = uuid.UUID(int=7), uuid.UUID(int=9)


def _ctx(text="退货要几天", kb_ids=(KB,), events=None):
    return RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", text)],
                      events=events or Events(), user_id=USER, knowledge_base_ids=tuple(kb_ids))


# ---------- 工具与引用（Agent 单元） ----------

async def test_searches_with_server_side_scope_and_returns_marked_citations():
    events = Events()
    search = RecordingSearch()
    agent = ChatbotAgent(ChatbotContext(model=FakeEchoChatModel(), knowledge_search=search))
    reply = await agent.run(_ctx(events=events))
    assert search.calls == [(USER, [KB], "退货要几天")]  # 身份和范围来自 RunContext
    assert reply.content == "echo: 退货要几天 [1]"
    assert [(c["n"], c["filename"], c["page"], c["cited"]) for c in reply.citations] == [
        (1, "退货.txt", None, True), (2, "手册.pdf", 3, False)]
    assert events.of(TOOL_STARTED) == [{"name": "knowledge_search", "query": "退货要几天"}]
    assert events.of(TOOL_FINISHED) == [{"name": "knowledge_search", "hits": 2, "ok": True}]
    kinds = [t for t, _ in events.items]
    assert kinds.index(TOOL_FINISHED) < kinds.index(TOKEN)  # 先检索完，再流出回答


async def test_no_tool_without_linked_knowledge_bases_or_search():
    search = RecordingSearch()
    model = ScriptedModel(script=[AIMessage(content="你好")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search)).run(_ctx(kb_ids=()))
    assert (reply.content, reply.citations, search.calls) == ("你好", None, [])
    assert model._calls[0][1] is False  # 没关联知识库：不绑定工具，提示词里也没有检索说明
    assert "knowledge_search" not in model._calls[0][0][0].content


async def test_model_cannot_choose_knowledge_bases():
    """工具参数里塞进别的知识库 id（例如提示注入）也没用：范围只由服务端决定。"""
    search = RecordingSearch()
    other = str(uuid.uuid4())
    model = ScriptedModel(script=[_call("退货", knowledge_base_ids=[other], user_id=other), AIMessage(content="答 [1]")])
    await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search)).run(_ctx())
    assert search.calls == [(USER, [KB], "退货")]


async def test_numbering_continues_across_searches_and_reuses_numbers_for_repeated_chunks():
    search = RecordingSearch(results=[[_hit(1), _hit(2)], [_hit(2), _hit(3)]])
    model = ScriptedModel(script=[_call("退货", 0), _call("定制商品退货", 1), AIMessage(content="定制商品不能退 [3]")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search)).run(_ctx())
    second_tool_result = [m for m in model._calls[2][0] if isinstance(m, ToolMessage)][-1].content
    assert second_tool_result.startswith("[2] 来源") and "[3] 来源" in second_tool_result  # 重复的片段沿用编号 2
    assert [(c["n"], c["chunk_id"][-1], c["cited"]) for c in reply.citations] == [(1, "1", False), (2, "2", False), (3, "3", True)]


async def test_tool_rounds_are_capped_and_runaway_models_stop():
    search = RecordingSearch(results=[[_hit(i)] for i in range(1, 10)])
    model = ScriptedModel(script=[_call(f"q{i}", i) for i in range(4)] + [AIMessage(content="根据 [1] 回答")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search, max_tool_rounds=3)).run(_ctx())
    assert len(search.calls) == 3  # 第 4 次调用被拒绝，不再检索
    fourth = [m for m in model._calls[4][0] if isinstance(m, ToolMessage)][-1]
    assert fourth.content == ROUND_LIMIT_REACHED and reply.content == "根据 [1] 回答"

    stubborn = ScriptedModel(script=[_call("again")])  # 无视上限一直调用工具：图在有限步数后报错，不会无限花钱
    with pytest.raises(GraphRecursionError):
        await ChatbotAgent(ChatbotContext(model=stubborn, knowledge_search=RecordingSearch(), max_tool_rounds=2)).run(_ctx())
    assert len(stubborn._calls) <= 2 + 2 + 1


async def test_search_failure_is_reported_to_the_model_not_raised():
    events = Events()
    model = ScriptedModel(script=[_call("退货"), AIMessage(content="抱歉，知识库暂时不可用")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=RecordingSearch(fail=True))).run(_ctx(events=events))
    assert reply.content == "抱歉，知识库暂时不可用" and reply.citations is None
    assert events.of(TOOL_FINISHED) == [{"name": "knowledge_search", "hits": 0, "ok": False}]


def test_formatting_and_cited_marking():
    assert format_for_model([]) == NO_RESULT
    text = format_for_model([(1, _hit(1)), (2, _hit(2, filename="手册.pdf", page=3))])
    assert text == "[1] 来源：退货.txt\n片段1的内容\n\n[2] 来源：手册.pdf（第 3 页）\n片段2的内容"
    cits = [{"n": 1}, {"n": 2}, {"n": 12}]
    assert [c["cited"] for c in mark_cited(cits, "见 [2]，另见[12]。2024 年[2]")] == [False, True, True]


# ---------- 对话关联知识库（接口） ----------

@pytest_asyncio.fixture
async def stack(sessionmaker, tmp_path, chunk_index, redis):
    """真实的数据库 + Qdrant 进程内模式 + 假向量化 + 假模型：走完整的 HTTP → 派发 → Agent → 落库链路。"""
    from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader

    knowledge = KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index)
    search = SearchService(sessionmaker, FakeEmbedder(), chunk_index, None)

    async def knowledge_search(user_id, kb_ids, query):
        return (await search.search(user_id, kb_ids, query, top_k=3)).hits

    agent = ChatbotAgent(ChatbotContext(model=FakeEchoChatModel(), knowledge_search=knowledge_search))
    dispatcher = RunDispatcher(sessionmaker, agent.run, sweep_interval=60, events=RunEventPublisher(redis))
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    await dispatcher.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c, knowledge, RunEventReader(redis)
    finally:
        await dispatcher.stop()


async def _user(c):
    return {"X-User-Id": (await c.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]}


async def _wait_run(c, h, run_id):
    for _ in range(200):
        run = (await c.get(f"/api/runs/{run_id}", headers=h)).json()
        if run["status"] in ("succeeded", "failed", "cancelled"):
            return run
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


async def test_linking_knowledge_bases_checks_ownership(stack):
    c, knowledge, _ = stack
    h, other = await _user(c), await _user(c)
    uid = uuid.UUID(h["X-User-Id"])
    kb1 = await knowledge.create_kb(uid, "a", None)
    kb2 = await knowledge.create_kb(uid, "b", None)
    others_kb = await knowledge.create_kb(uuid.UUID(other["X-User-Id"]), "x", None)
    cid = (await c.post("/api/conversations", json={}, headers=h)).json()["id"]
    url = f"/api/conversations/{cid}/knowledge-bases"
    assert (await c.get(url, headers=h)).json() == {"knowledge_base_ids": []}
    r = await c.put(url, json={"knowledge_base_ids": [str(kb2.id), str(kb1.id), str(kb2.id)]}, headers=h)
    assert sorted(r.json()["knowledge_base_ids"]) == sorted([str(kb1.id), str(kb2.id)])  # 去重
    r = await c.put(url, json={"knowledge_base_ids": [str(kb1.id), str(others_kb.id)]}, headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "knowledge_base_not_found")
    assert len((await c.get(url, headers=h)).json()["knowledge_base_ids"]) == 2  # 失败时原关联不变
    assert (await c.get(url, headers=other)).status_code == 404  # 别人的对话
    assert (await c.put(url, json={"knowledge_base_ids": [str(uuid.uuid4()) for _ in range(11)]}, headers=h)).status_code == 422
    assert (await c.put(url, json={"knowledge_base_ids": []}, headers=h)).json() == {"knowledge_base_ids": []}


async def test_end_to_end_answer_with_citations_is_persisted(stack):
    c, knowledge, reader = stack
    h = await _user(c)
    uid = uuid.UUID(h["X-User-Id"])
    kb = await knowledge.create_kb(uid, "客服", None)
    doc = await knowledge.upload(uid, kb.id, "退货.txt", "退货政策：签收后七天内可以无理由退货。".encode())
    await knowledge.process(doc.id)
    cid = (await c.post("/api/conversations", json={}, headers=h)).json()["id"]

    run = (await c.post(f"/api/conversations/{cid}/messages", json={"content": "退货政策"}, headers=h)).json()
    await _wait_run(c, h, run["id"])
    plain = (await c.get(f"/api/conversations/{cid}/messages", headers=h)).json()[-1]
    assert plain["citations"] is None  # 没关联知识库：不检索

    await c.put(f"/api/conversations/{cid}/knowledge-bases", json={"knowledge_base_ids": [str(kb.id)]}, headers=h)
    run = (await c.post(f"/api/conversations/{cid}/messages", json={"content": "退货政策"}, headers=h)).json()
    assert (await _wait_run(c, h, run["id"]))["status"] == "succeeded"
    answer = (await c.get(f"/api/conversations/{cid}/messages", headers=h)).json()[-1]
    assert answer["content"] == "echo: 退货政策 [1]"
    assert len(answer["citations"]) == 1
    cit = answer["citations"][0]
    assert (cit["n"], cit["filename"], cit["document_id"], cit["cited"]) == (1, "退货.txt", str(doc.id), True)
    assert "七天内" in cit["text"]
    types = [e.type for e in await reader.read(uuid.UUID(run["id"]), "0-0", None)]
    assert types[:3] == ["run.started", TOOL_STARTED, TOOL_FINISHED] and types[-1] == RUN_COMPLETED


# ---------- 预检索 ----------

async def test_prefetch_searches_with_the_raw_question_before_the_model():
    events = Events()
    search = RecordingSearch()
    model = ScriptedModel(script=[AIMessage(content="7 天内可以退 [1]")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search, prefetch=True)).run(_ctx(events=events))
    assert search.calls == [(USER, [KB], "退货要几天")]
    assert len(model._calls) == 1  # 拿到结果直接回答：只调用一次模型
    seen = model._calls[0][0]
    assert isinstance(seen[-2], AIMessage) and seen[-2].tool_calls[0]["args"] == {"query": "退货要几天"}
    assert isinstance(seen[-1], ToolMessage) and seen[-1].content.startswith("[1] 来源：退货.txt")
    assert "系统已经用用户的原话检索过一次" in seen[0].content
    assert [c["cited"] for c in reply.citations] == [True, False]
    assert events.of(TOOL_STARTED) == [{"name": "knowledge_search", "query": "退货要几天"}]
    assert events.of(TOOL_FINISHED) == [{"name": "knowledge_search", "hits": 2, "ok": True}]


async def test_prefetch_counts_as_a_round_and_the_model_can_still_search_again():
    search = RecordingSearch(results=[[_hit(1)], [_hit(2)], [_hit(3)]])
    model = ScriptedModel(script=[_call("定制商品能否退货", 1), _call("再查一次", 2), AIMessage(content="不能退 [2]")])
    reply = await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search, prefetch=True, max_tool_rounds=2)).run(
        _ctx(text="那定制的呢"))
    assert [q for _, _, q in search.calls] == ["那定制的呢", "定制商品能否退货"]  # 第三次超过上限（预检索算第一轮）
    assert [(c["n"], c["cited"]) for c in reply.citations] == [(1, False), (2, True)]


async def test_no_prefetch_without_linked_knowledge_bases():
    search = RecordingSearch()
    model = ScriptedModel(script=[AIMessage(content="你好")])
    await ChatbotAgent(ChatbotContext(model=model, knowledge_search=search, prefetch=True)).run(_ctx(kb_ids=()))
    assert search.calls == [] and len(model._calls[0][0]) == 2  # 系统提示 + 用户消息，没有检索结果


async def test_fake_model_with_prefetch_answers_without_a_second_search():
    search = RecordingSearch()
    reply = await ChatbotAgent(ChatbotContext(model=FakeEchoChatModel(), knowledge_search=search, prefetch=True)).run(_ctx())
    assert reply.content == "echo: 退货要几天 [1]" and len(search.calls) == 1
