"""实验 4：用量怎么采集——在图的配置里挂一个回调，能不能捕获节点内部的每一次模型调用？（decisions/0023 草稿）

4.3 的做法是把用量写进图状态，崩溃那次执行中“在途”的调用会丢。回调在每次调用结束时触发，与图是否提交无关。
要确认的是：节点里直接 `model.ainvoke(messages)`（没有显式传 config）、结构化输出、bind_tools、
Send 并行的子任务、流式调用，回调都能收到吗？
用假模型，零费用。用法：python callback_capture.py（需要后端依赖）
"""

import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel


class Fake(BaseChatModel):
    tag: str = "m"

    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kw):
        return self.bind(tools=tools, **kw)  # 把 tools / tool_choice 传给 _agenerate（结构化输出靠它们）

    def _usage(self):
        return {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        if kw.get("tools") or "tool_choice" in kw:
            msg = AIMessage(content="", tool_calls=[{"name": "Plan", "args": {"items": ["a", "b", "c"]}, "id": "1"}],
                            usage_metadata=self._usage())
        else:
            msg = AIMessage(content="ok", usage_metadata=self._usage())
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _astream(self, messages, stop=None, run_manager=None, **kw):
        for i, part in enumerate(["o", "k"]):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part, usage_metadata=self._usage() if i == 1 else None))

    def _generate(self, *a, **k):
        raise NotImplementedError


class Plan(BaseModel):
    items: list[str]


class Recorder(AsyncCallbackHandler):
    def __init__(self):
        self.calls = []

    async def on_llm_end(self, response, *, run_id, parent_run_id=None, tags=None, **kw):
        msg = response.generations[0][0].message
        self.calls.append(((kw.get("metadata") or {}).get("langgraph_node"), msg.usage_metadata))


class S(TypedDict, total=False):
    items: list[str]
    out: Annotated[list[str], operator.add]


async def main():
    model = Fake()
    structured = Fake().with_structured_output(Plan)

    async def plan(state: S):
        # 结构化输出（with_structured_output），不显式传 config
        return {"items": (await structured.ainvoke([HumanMessage("拆任务")])).items}

    async def sub(task: dict):
        await model.bind_tools([]).ainvoke([HumanMessage(task["item"])])  # bind_tools，不传 config
        return {"out": [task["item"]]}

    async def write(state: S):
        text = "".join([c.content async for c in model.astream([HumanMessage("汇总")])])  # 流式
        return {"out": [text]}

    g = StateGraph(S)
    g.add_node("plan", plan)
    g.add_node("sub", sub)
    g.add_node("write", write)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: [Send("sub", {"item": i}) for i in s["items"]], ["sub"])
    g.add_edge("sub", "write")
    g.add_edge("write", END)
    rec = Recorder()
    await g.compile().ainvoke({"out": []}, {"callbacks": [rec]})
    print(f"捕获到 {len(rec.calls)} 次模型调用（期望 1 次计划 + 3 个子任务 + 1 次汇总 = 5）")
    for node, usage in rec.calls:
        print(f"  节点 {node}: {usage}")


asyncio.run(main())
