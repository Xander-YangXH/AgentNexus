"""通用工具与 Skills（阶段 4.2，decisions/0021）：按技能启用工具、执行前的许可检查、工作空间由服务端决定、失败与上限。

模型用剧本代替（不调用真实模型）；工具用记录调用的假工具。沙盒和 MCP 本身的行为见 test_sandbox.py、test_mcp_gateway.py。
"""

import uuid
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphRecursionError
from pydantic import PrivateAttr

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.agents.buildin.chatbot.graph import ROUND_LIMIT_REACHED
from agentnexus.agents.skills import DEFAULT_DIR, LOAD_SKILL, Skill, load_skills, parse_skill
from agentnexus.agents.tools import OUTPUT_LIMIT, AgentTool, StaticTools, ToolError, truncate
from agentnexus.agents.tools.sandbox import sandbox_tools
from agentnexus.agents.types import ChatMessage, RunContext
from agentnexus.events import TOOL_FINISHED, TOOL_STARTED
from agentnexus.sandbox.client import SandboxError


class ScriptedModel(BaseChatModel):
    """按剧本依次返回消息；记录每次调用时绑定了哪些工具、系统提示是什么。"""

    script: list
    _calls: list = PrivateAttr(default_factory=list)
    _bound: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        bound = self.model_copy()
        bound._calls, bound._bound = self._calls, [t["function"]["name"] for t in tools]
        return bound

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append({"tools": list(self._bound), "system": messages[0].content, "messages": list(messages)})
        step = self.script[min(len(self._calls) - 1, len(self.script) - 1)]
        return ChatResult(generations=[ChatGeneration(message=step.model_copy(update={"id": None}))])


def _call(tool, i=0, **args):
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": f"c{i}", "type": "tool_call"}])


class FakeTool:
    def __init__(self, name, result="ok", fail=None):
        self.name, self.result, self.fail, self.calls = name, result, fail, []

    async def run(self, args, scope):
        self.calls.append((args, scope))
        if self.fail:
            raise self.fail
        return self.result

    def tool(self):
        return AgentTool(self.name, f"{self.name} 工具", {"type": "object", "properties": {}}, self.run)


SKILLS = {
    "data-analysis": Skill("data-analysis", "分析数据", ("python_exec", "list_files"), "数据分析的完整说明"),
    "web-research": Skill("web-research", "抓网页", ("fetch",), "网页说明"),
}


class Recorder:
    def __init__(self):
        self.events = []

    async def emit(self, type_, data=None):
        self.events.append((type_, data or {}))


async def _run(script, tools, *, skills=SKILLS, **ctx_kw):
    model = ScriptedModel(script=script)
    agent = ChatbotAgent(ChatbotContext(model=model, tool_sources=[StaticTools([t.tool() for t in tools])],
                                        skills=skills, **ctx_kw))
    events = Recorder()
    ctx = RunContext(run_id=uuid.uuid4(), conversation_id=uuid.uuid4(), history=[ChatMessage("user", "分析一下")],
                     events=events)
    reply = await agent.run(ctx)
    return reply, model._calls, events.events, ctx


def _tool_messages(call):
    return [m for m in call["messages"] if isinstance(m, ToolMessage)]


# ---------- Skills ----------

async def test_tools_are_offered_only_after_their_skill_is_loaded():
    exec_tool, ls = FakeTool("python_exec", "42"), FakeTool("list_files")
    script = [_call(LOAD_SKILL, name="data-analysis"), _call("python_exec", 1, code="print(42)"), AIMessage(content="答案是 42")]
    reply, calls, events, ctx = await _run(script, [exec_tool, ls])

    assert reply.content == "答案是 42"
    # 第一次调用：只有 load_skill，提示里列出技能；fetch 不可用，所以 web-research 不出现
    assert calls[0]["tools"] == [LOAD_SKILL]
    assert "data-analysis：分析数据" in calls[0]["system"] and "web-research" not in calls[0]["system"]
    # 加载之后：技能列出的工具才被绑定；load_skill 的结果是技能正文
    assert calls[1]["tools"] == [LOAD_SKILL, "python_exec", "list_files"]
    assert _tool_messages(calls[1])[0].content.startswith("数据分析的完整说明")
    # 工作空间是服务端决定的当前对话，不来自模型参数
    assert exec_tool.calls == [({"code": "print(42)"}, exec_tool.calls[0][1])]
    assert exec_tool.calls[0][1].workspace_id == str(ctx.conversation_id)
    started = [d for t, d in events if t == TOOL_STARTED]
    assert [(d["name"], d["detail"]) for d in started] == [(LOAD_SKILL, "data-analysis"), ("python_exec", "print(42)")]
    assert [d["ok"] for t, d in events if t == TOOL_FINISHED] == [True, True]


async def test_tools_of_unloaded_skills_are_refused_even_if_called():
    """模型“猜”着调用、或被注入的内容诱导调用没启用的工具：执行前拒绝，工具不会被执行。"""
    exec_tool = FakeTool("python_exec")
    script = [_call("python_exec", code="import os"), _call("fetch", 1, url="http://x"), AIMessage(content="好")]
    _, calls, events, _ = await _run(script, [exec_tool, FakeTool("list_files")])
    assert exec_tool.calls == []
    first, second = _tool_messages(calls[1])[0], _tool_messages(calls[2])[1]
    assert first.status == "error" and "load_skill" in first.content
    assert second.status == "error" and "工具不可用：fetch" in second.content
    assert [d["ok"] for t, d in events if t == TOOL_FINISHED] == [False, False]


async def test_unknown_or_unavailable_skill_cannot_be_loaded():
    script = [_call(LOAD_SKILL, name="web-research"), AIMessage(content="好")]
    _, calls, _, _ = await _run(script, [FakeTool("python_exec"), FakeTool("list_files")])
    msg = _tool_messages(calls[1])[0]
    assert msg.status == "error" and "没有这个技能" in msg.content and "data-analysis" in msg.content
    assert calls[1]["tools"] == [LOAD_SKILL]  # 没加载成功，不启用任何工具


async def test_no_skills_means_no_tools_bound():
    _, calls, _, _ = await _run([AIMessage(content="你好")], [FakeTool("python_exec")], skills={})
    assert calls[0]["tools"] == [] and "load_skill" not in calls[0]["system"]


async def test_tool_failure_is_reported_to_the_model_not_raised():
    failing = FakeTool("python_exec", fail=ToolError("沙盒拒绝或执行失败（400: path escapes workspace）"))
    script = [_call(LOAD_SKILL, name="data-analysis"), _call("python_exec", 1, code="x"), AIMessage(content="执行失败了")]
    reply, calls, _, _ = await _run(script, [failing, FakeTool("list_files")])
    msg = _tool_messages(calls[2])[-1]
    assert msg.status == "error" and "path escapes workspace" in msg.content
    assert reply.content == "执行失败了"


async def test_loading_a_skill_raises_the_round_limit_but_it_still_stops():
    exec_tool = FakeTool("python_exec")
    script = [_call(LOAD_SKILL, name="data-analysis")] + [_call("python_exec", i, code="loop") for i in range(1, 30)]
    with pytest.raises(GraphRecursionError):  # 模型无视上限一直调用：最终由 recursion_limit 兜底
        await _run(script, [exec_tool, FakeTool("list_files")], max_tool_rounds=3, max_skill_tool_rounds=5)
    assert len(exec_tool.calls) == 4  # 第 1 轮 load_skill，第 2～5 轮执行，之后都被拒绝


async def test_round_limit_message_is_returned_after_limit():
    exec_tool = FakeTool("python_exec")
    script = [_call(LOAD_SKILL, name="data-analysis"), _call("python_exec", 1, code="a"), _call("python_exec", 2, code="b"),
              AIMessage(content="完成")]
    _, calls, _, _ = await _run(script, [exec_tool, FakeTool("list_files")], max_skill_tool_rounds=2)
    assert len(exec_tool.calls) == 1
    assert _tool_messages(calls[3])[-1].content == ROUND_LIMIT_REACHED


def test_parse_skill_validates_the_header():
    skill = parse_skill("---\nname: demo\ndescription: 演示\ntools: [a, b]\n---\n# 正文\n")
    assert skill == Skill("demo", "演示", ("a", "b"), "# 正文")
    for bad in ("没有文件头", "---\nname: Bad Name\ndescription: x\n---\n", "---\nname: ok\n---\n",
                "---\nname: ok\ndescription: x\ntools: python_exec\n---\n"):
        with pytest.raises(ValueError):
            parse_skill(bad)


def test_bundled_skills_load_and_reference_real_tools(tmp_path):
    skills = load_skills(DEFAULT_DIR)
    assert set(skills) >= {"data-analysis", "web-research"}
    known = {"python_exec", "list_files", "read_file", "write_file", "fetch"}  # fetch 来自 MCP 网关
    for skill in skills.values():
        assert set(skill.tools) <= known, skill.name
    (tmp_path / "a.md").write_text("---\nname: dup\ndescription: x\n---\n")
    (tmp_path / "b.md").write_text("---\nname: dup\ndescription: y\n---\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_skills(tmp_path)
    assert Path(DEFAULT_DIR).name == "skills"


# ---------- 沙盒工具（假的 sandbox 客户端） ----------

class FakeSandbox:
    def __init__(self, exec_result=None, error=None):
        self.exec_result, self.error, self.calls = exec_result, error, []

    async def execute(self, ws, code, timeout=30.0):
        self.calls.append(("execute", ws, code, timeout))
        if self.error:
            raise self.error
        return self.exec_result

    async def list_files(self, ws):
        return [{"path": "data.csv", "size": 12}]


def _scope():
    from agentnexus.agents.tools import ToolScope

    return ToolScope(workspace_id=str(uuid.uuid4()))


async def test_python_exec_formats_results():
    cases = [
        ({"exit_code": 0, "stdout": "42\n", "stderr": "", "timed_out": False, "oom": False}, ["退出码 0", "stdout:\n42"]),
        ({"exit_code": None, "stdout": "", "stderr": "", "timed_out": True, "oom": False}, ["超时"]),
        ({"exit_code": 137, "stdout": "", "stderr": "", "timed_out": False, "oom": True}, ["内存不足"]),
        ({"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "oom": False}, ["没有输出"]),
    ]
    for result, expected in cases:
        box = FakeSandbox(result)
        exec_tool = {t.name: t for t in sandbox_tools(box, exec_timeout=7)}["python_exec"]
        text = await exec_tool.run({"code": "x"}, _scope())
        assert all(e in text for e in expected), text
        assert box.calls[0][3] == 7  # 超时由配置决定，模型改不了


async def test_sandbox_tools_need_a_workspace_and_wrap_errors():
    tools = {t.name: t for t in sandbox_tools(FakeSandbox(error=SandboxError("400: path escapes workspace")))}
    with pytest.raises(ToolError, match="缺少工作空间"):
        await tools["python_exec"].run({"code": "x"}, None)
    with pytest.raises(ToolError, match="path escapes workspace"):
        await tools["python_exec"].run({"code": "x"}, _scope())
    with pytest.raises(ToolError, match="缺少参数"):
        await tools["python_exec"].run({}, _scope())
    assert await tools["list_files"].run({}, _scope()) == "data.csv（12 字节）"


def test_truncate_keeps_short_text_and_marks_long_text():
    assert truncate("abc") == "abc"
    long = truncate("x" * (OUTPUT_LIMIT + 10))
    assert long.startswith("x" * OUTPUT_LIMIT) and "已截断" in long and str(OUTPUT_LIMIT + 10) in long
