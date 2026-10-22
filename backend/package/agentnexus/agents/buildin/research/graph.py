"""深度调研图（decisions/0022）：制定计划 → 等待审批 → 并行子 Agent → 汇总成报告。

两条由实验得出的规则（backend/eval/multiagent_exp）：
- 每个子任务必须是一个独立的任务（Send）：同一步里并行的任务各自把结果写进 checkpoint，worker 崩溃后只重做没完成的；
  写在一个节点里循环，崩溃后全部重做。
- 恢复时含 interrupt() 的节点会从头执行：所以“调用模型制定计划”和“等待审批”是两个节点。写在一起的话，恢复时会再调用一次模型，
  真实模型每次输出不同——用户批准的计划和实际执行的计划就不是同一个了。
"""

import logging
import operator
import uuid
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from agentnexus.agents.buildin.chatbot.tools import KNOWLEDGE_SEARCH, KNOWLEDGE_SEARCH_TOOL, MAX_QUERY_CHARS
from agentnexus.agents.buildin.research.prompt import SUBAGENT_PROMPT, WRITER_PROMPT
from agentnexus.agents.messages import message_text
from agentnexus.agents.tools import ToolScope, truncate
from agentnexus.events import RESEARCH_SUBTASK, TOOL_FINISHED, TOOL_STARTED

log = logging.getLogger(__name__)

SUBAGENT_OUTPUT_LIMIT = 4000  # 子 Agent 每次工具结果给模型的字符上限（比聊天更小：子任务多、并行）
OUT_OF_ROUNDS = "工具调用次数已用完。请根据已经得到的资料，直接给出这个子任务的结论。"


class ResearchState(TypedDict, total=False):
    request: str  # 用户的调研任务（本轮最后一条用户消息）
    background: str  # 之前的对话（截断），帮助理解指代
    plan: list[dict]  # [{title, question}]
    feedback: str | None  # 用户不批准时给的修改意见
    approved: bool
    findings: Annotated[list[dict], operator.add]  # 每个子 Agent 一条：{index, title, summary, sources, ok}
    report: str
    attachment: dict | None
    usage: Annotated[list[dict], operator.add]  # 每次模型调用的用量（汇总进日志）


def _emit(type_: str, data: dict) -> None:
    """通过 LangGraph 的自定义流发事件，由 ResearchAgent 转发到 run 的事件流（节点里拿不到 RunContext）。"""
    get_stream_writer()({"type": type_, "data": data})


def _usage(message) -> dict:
    return dict(getattr(message, "usage_metadata", None) or {})


def build_graph(context, checkpointer=None):
    async def plan(state: ResearchState):
        request = state["request"]
        if state.get("background"):  # 之前的对话只用来理解指代（“上面说的那三家”）
            request = f"{request}\n\n（之前的对话，供理解上下文）\n{state['background']}"
        result = await context.planner(request, state.get("feedback"), state.get("plan"))
        return {"plan": result.subtasks, "approved": False, "usage": [result.usage] if result.usage else []}

    def approve(state: ResearchState):
        # 只有 interrupt 和处理答复：恢复时本节点从头执行，这里不能有模型调用等有成本 / 副作用的操作
        n = len(state["plan"])
        decision = interrupt({
            "kind": "research_plan",
            "request": state["request"],
            "plan": state["plan"],
            "estimate": {"subtasks": n, "model_calls": f"{2 + 2 * n}~{2 + (context.subagent_max_rounds + 1) * n}"},
        })
        return {"approved": bool(decision.get("approved")), "feedback": decision.get("feedback")}

    def after_approval(state: ResearchState):
        if not state.get("approved"):
            return "plan"  # 带着修改意见重新制定计划，之后会再次请求审批
        return [Send("research", {"index": i, "subtask": s, "request": state["request"], "total": len(state["plan"])})
                for i, s in enumerate(state["plan"])]

    async def research(task: dict, config: RunnableConfig):
        """一个子 Agent：只看到自己的子任务，独立的消息上下文；最终只把结论交给汇总。"""
        index, subtask, total = task["index"], task["subtask"], task["total"]
        _emit(RESEARCH_SUBTASK, {"index": index, "title": subtask["title"], "status": "started", "total": total})
        usage: list[dict] = []
        try:
            summary, sources = await _run_subagent(context, task, config, usage)
            ok = True
        except Exception as exc:  # 一个子任务失败不拖垮整份报告：记下来，汇总时如实说明（取消是 CancelledError，不在这里）
            log.warning("research subtask %d failed: %s", index, exc, exc_info=True)
            summary, sources, ok = f"子任务执行失败（{type(exc).__name__}）。", [], False
        _emit(RESEARCH_SUBTASK, {"index": index, "title": subtask["title"], "status": "finished" if ok else "failed",
                                 "total": total})
        return {"findings": [{"index": index, "title": subtask["title"], "summary": summary, "sources": sources,
                              "ok": ok}], "usage": usage}

    async def write(state: ResearchState):
        findings = sorted(state.get("findings", []), key=lambda f: f["index"])
        parts = [f"## 子任务 {f['index'] + 1}：{f['title']}\n{f['summary']}"
                 + (f"\n来源：{'; '.join(f['sources'])}" if f["sources"] else "") for f in findings]
        reply = await context.model.ainvoke([
            SystemMessage(WRITER_PROMPT),
            HumanMessage(f"调研任务：{state['request']}\n\n各子任务的结论：\n\n" + "\n\n".join(parts)),
        ])
        report = message_text(reply).strip()
        attachment = None
        if context.save_artifact is not None and report:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            attachment = await context.save_artifact(f"调研报告-{stamp}.md", "text/markdown; charset=utf-8",
                                                     report.encode())
        return {"report": report, "attachment": attachment, "usage": [_usage(reply)]}

    graph = StateGraph(ResearchState)
    graph.add_node("plan", plan)
    graph.add_node("approve", approve)
    graph.add_node("research", research)
    graph.add_node("write", write)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "approve")
    graph.add_conditional_edges("approve", after_approval, ["plan", "research"])
    graph.add_edge("research", "write")
    graph.add_edge("write", END)
    return graph.compile(checkpointer=checkpointer)


async def _run_subagent(context, task: dict, config: RunnableConfig, usage: list[dict]) -> tuple[str, list[str]]:
    index, subtask = task["index"], task["subtask"]
    configurable = config["configurable"]
    kb_ids = [uuid.UUID(k) for k in configurable.get("knowledge_base_ids", [])]
    tools = {}
    for source in context.tool_sources:
        for tool in await source.tools():
            if tool.name in context.subagent_tools:  # 子 Agent 只能检索和抓取：没有执行代码、写文件
                tools.setdefault(tool.name, tool)
    specs = [t.spec() for t in tools.values()]
    if context.knowledge_search is not None and kb_ids:
        specs.insert(0, KNOWLEDGE_SEARCH_TOOL)
    model = context.model.bind_tools(specs) if specs else context.model
    messages = [SystemMessage(SUBAGENT_PROMPT),
                HumanMessage(f"总的调研任务：{task['request']}\n\n你的子任务：{subtask['title']}\n要回答的问题：{subtask['question']}")]
    sources: list[str] = []
    scope = ToolScope(workspace_id=configurable.get("workspace_id", ""), user_id=configurable.get("user_id"))

    for round_ in range(context.subagent_max_rounds + 1):
        if round_ == context.subagent_max_rounds:
            messages.append(HumanMessage(OUT_OF_ROUNDS))
        reply = await model.ainvoke(messages)
        usage.append(_usage(reply))
        messages.append(reply)
        if not (isinstance(reply, AIMessage) and reply.tool_calls) or round_ == context.subagent_max_rounds:
            return message_text(reply).strip() or "（没有给出结论）", list(dict.fromkeys(sources))
        for call in reply.tool_calls:
            args = call["args"] or {}
            detail = str(args.get("query") or args.get("url") or "")[:200]
            _emit(TOOL_STARTED, {"name": call["name"], "query": str(args.get("query", "")), "detail": detail,
                                 "subtask": index})
            ok, text = True, ""
            try:
                if call["name"] == KNOWLEDGE_SEARCH and context.knowledge_search is not None and kb_ids:
                    query = str(args.get("query", "")).strip()[:MAX_QUERY_CHARS]
                    hits = await context.knowledge_search(uuid.UUID(configurable["user_id"]), kb_ids, query)
                    sources += [h.filename for h in hits]
                    text = "\n\n".join(f"来源：{h.filename}\n{h.text}" for h in hits) or "知识库中没有找到相关内容。"
                elif call["name"] in tools:
                    text = await tools[call["name"]].run(args, scope)
                    if args.get("url"):
                        sources.append(str(args["url"]))
                else:
                    ok, text = False, f"工具不可用：{call['name']}"
            except Exception as exc:
                ok, text = False, f"工具执行失败：{exc}"[:500]
            _emit(TOOL_FINISHED, {"name": call["name"], "hits": 0, "ok": ok, "subtask": index})
            messages.append(ToolMessage(truncate(text, SUBAGENT_OUTPUT_LIMIT), tool_call_id=call["id"], name=call["name"],
                                        status="success" if ok else "error"))
    raise AssertionError("unreachable")
