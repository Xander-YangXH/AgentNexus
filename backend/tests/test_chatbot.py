import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import Field

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.agents.messages import message_text, to_langchain
from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.config import Settings
from agentnexus.models import build_chat_model
from agentnexus.models.factory import ModelConfigError
from agentnexus.models.fake import FakeEchoChatModel
from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus


class RecordingModel(FakeEchoChatModel):
    """记录每次收到的消息；可配置前 N 次调用失败。"""

    calls: list = Field(default_factory=list)
    fail_times: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("llm down")
        return super()._generate(messages, stop, run_manager, **kwargs)


def _ctx(history, run_id=None, conv_id=None):
    return RunContext(
        run_id=run_id or uuid.uuid4(),
        conversation_id=conv_id or uuid.uuid4(),
        history=[ChatMessage(r, c) for r, c in history],
    )


def _cfg(run_id):
    return {"configurable": {"thread_id": str(run_id)}}


# ---------- 消息映射 ----------

def test_to_langchain_maps_roles_in_order():
    msgs = to_langchain([ChatMessage("system", "s"), ChatMessage("user", "u"), ChatMessage("assistant", "a")])
    assert [type(m) for m in msgs] == [SystemMessage, HumanMessage, AIMessage]
    assert [m.content for m in msgs] == ["s", "u", "a"]


@pytest.mark.parametrize("role", ["tool", "bogus"])
def test_to_langchain_rejects_unexpected_roles(role):
    with pytest.raises(ValueError):
        to_langchain([ChatMessage(role, "x")])


def test_message_text_handles_string_and_content_blocks():
    assert message_text(AIMessage("hi")) == "hi"
    blocks = [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "1", "name": "t", "input": {}}, {"type": "text", "text": "b"}]
    assert message_text(AIMessage(content=blocks)) == "ab"


# ---------- 模型工厂（只测配置分支与构造，不联网） ----------

def _settings(**kw):
    return Settings(database_url="x", redis_url="y", **kw)


def test_factory_default_is_fake():
    assert isinstance(build_chat_model(_settings()), FakeEchoChatModel)


@pytest.mark.parametrize(
    "kw",
    [
        {"llm_provider": "openai", "llm_api_key": "k"},  # 缺 model
        {"llm_provider": "openai", "llm_model": "m"},  # 缺 key
        {"llm_provider": "anthropic", "llm_model": "m"},
        {"llm_provider": "nope"},
    ],
)
def test_factory_rejects_bad_config(kw):
    with pytest.raises(ModelConfigError):
        build_chat_model(_settings(**kw))


def test_factory_builds_real_provider_clients_without_network():
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI

    m = build_chat_model(_settings(llm_provider="openai", llm_model="m", llm_api_key="k", llm_base_url="http://localhost:1/v1"))
    assert isinstance(m, ChatOpenAI) and m.openai_api_base == "http://localhost:1/v1"
    assert isinstance(build_chat_model(_settings(llm_provider="anthropic", llm_model="m", llm_api_key="k")), ChatAnthropic)


# ---------- Agent / 图 ----------

async def test_agent_feeds_full_history_with_system_prompt_first(checkpointer):
    model = RecordingModel()
    agent = ChatbotAgent(ChatbotContext(model=model, system_prompt="SYS"), checkpointer)
    reply = await agent.run(_ctx([("user", "u1"), ("assistant", "a1"), ("user", "u2")]))

    assert reply == "echo: u2"
    seen = model.calls[0]
    assert [type(m) for m in seen] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]
    assert seen[0].content == "SYS"


async def test_checkpoint_thread_is_run_id_and_excludes_system_prompt(checkpointer):
    agent = ChatbotAgent(ChatbotContext(model=RecordingModel()), checkpointer)
    ctx = _ctx([("user", "hi")])
    await agent.run(ctx)

    saved = await checkpointer.aget_tuple(_cfg(ctx.run_id))
    msgs = saved.checkpoint["channel_values"]["messages"]
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage]  # 没有 SystemMessage
    # 以 conversation_id 为 thread 查不到任何东西 —— 不会按对话累积
    assert await checkpointer.aget_tuple(_cfg(ctx.conversation_id)) is None


async def test_runs_of_same_conversation_do_not_accumulate_in_checkpoint(checkpointer):
    model = RecordingModel()
    agent = ChatbotAgent(ChatbotContext(model=model), checkpointer)
    conv_id = uuid.uuid4()
    history = [("user", "u1")]
    await agent.run(_ctx(history, conv_id=conv_id))
    await agent.run(_ctx(history + [("assistant", "echo: u1"), ("user", "u2")], conv_id=conv_id))
    # 第二次模型只看到“表里的历史”(system + 3 条)，没有叠加第一次 checkpoint 里的内容
    assert len(model.calls[1]) == 1 + 3


async def test_failed_run_can_resume_from_checkpoint_without_resending_input(checkpointer):
    model = RecordingModel(fail_times=1)
    agent = ChatbotAgent(ChatbotContext(model=model), checkpointer)
    ctx = _ctx([("user", "retry me")])

    with pytest.raises(RuntimeError):
        await agent.run(ctx)
    state = await agent._graph.aget_state(_cfg(ctx.run_id))
    assert state.next == ("model",)  # 停在失败的节点上

    result = await agent._graph.ainvoke(None, _cfg(ctx.run_id))  # 阶段 4 的“接管续跑”
    assert [m.content for m in result["messages"]] == ["retry me", "echo: retry me"]


# ---------- 经过派发器的端到端 ----------

async def test_end_to_end_second_turn_sees_history_from_messages_table(sessionmaker, user, checkpointer):
    model = RecordingModel()
    agent = ChatbotAgent(ChatbotContext(model=model), checkpointer)
    d = RunDispatcher(sessionmaker, agent.run, sweep_interval=0.1)
    svc = RunService(sessionmaker)
    async with sessionmaker() as session, session.begin():
        conv = await ConversationRepository(session).create(user.id)

    async def turn(text):
        run = await svc.submit(user.id, conv.id, text)
        d.enqueue(run.id)
        for _ in range(250):
            async with sessionmaker() as session:
                got = await AgentRunRepository(session).get(run.id)
            if got.status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
                return got
            await asyncio.sleep(0.02)
        raise AssertionError("run did not finish")

    await d.start()
    try:
        assert (await turn("你好")).status == RunStatus.SUCCEEDED
        assert (await turn("再来")).status == RunStatus.SUCCEEDED
    finally:
        await d.stop()

    second_call = model.calls[1]
    assert [m.content for m in second_call[1:]] == ["你好", "echo: 你好", "再来"]
