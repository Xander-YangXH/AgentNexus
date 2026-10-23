"""深度调研 Agent（decisions/0022）。图见 graph.py；这里负责：从哪里开始（新任务 / 审批后继续 / 接管续做）、
把图里的事件转发到 run 的事件流、把“停在审批”翻译成 ApprovalRequired 交给派发器。"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.types import Command

from agentnexus.agents.base import BaseAgent
from agentnexus.agents.buildin.chatbot.tools import KnowledgeSearch
from agentnexus.agents.buildin.research.graph import build_graph
from agentnexus.agents.buildin.research.planner import Planner
from agentnexus.agents.messages import message_text
from agentnexus.agents.tools import ToolSource
from agentnexus.agents.types import AgentReply, ApprovalRequired, RunContext
from agentnexus.events import TOKEN
from agentnexus.services.usage import UsageRecorder

log = logging.getLogger(__name__)

BACKGROUND_CHARS = 2000  # 之前的对话最多带多少字给计划（只用来理解指代，不需要全文）

SaveArtifact = Callable[[str, str, bytes], Awaitable[dict]]


@dataclass
class ResearchContext:
    model: BaseChatModel  # 子 Agent 与汇总
    planner: Planner
    knowledge_search: KnowledgeSearch | None = None
    tool_sources: Sequence[ToolSource] = ()
    subagent_tools: frozenset[str] = frozenset({"fetch"})  # 子 Agent 能用的通用工具（另有对话关联知识库时的检索）
    save_artifact: SaveArtifact | None = None
    subagent_max_rounds: int = 4
    concurrency: int = 3  # 同时调用模型的子 Agent 数（max_concurrency）
    recursion_limit: int = 50
    usage: UsageRecorder | None = None  # 用量采集（decisions/0023）


class ResearchAgent(BaseAgent):
    name = "research"

    def __init__(self, context: ResearchContext, checkpointer=None):
        self._context = context
        self._graph = build_graph(context, checkpointer)

    async def run(self, ctx: RunContext) -> AgentReply | ApprovalRequired:
        config = {
            "configurable": {
                "thread_id": str(ctx.run_id),
                "user_id": str(ctx.user_id) if ctx.user_id else None,
                "knowledge_base_ids": [str(k) for k in ctx.knowledge_base_ids],
                "workspace_id": str(ctx.conversation_id),
            },
            "max_concurrency": self._context.concurrency,
            "recursion_limit": self._context.recursion_limit,
        }
        if self._context.usage is not None:
            config["callbacks"] = [self._context.usage.callback(ctx.user_id, "research", ctx.run_id)]
        graph_input = await self._starting_point(ctx, config)
        if isinstance(graph_input, (AgentReply, ApprovalRequired)):
            return graph_input

        async for mode, payload in self._graph.astream(graph_input, config, stream_mode=["messages", "custom"]):
            if mode == "custom":
                await ctx.events.emit(payload["type"], payload["data"])
            else:
                chunk, metadata = payload
                # 只流式显示汇总节点的输出：计划（结构化输出）和子 Agent 的过程不是给用户看的正文
                if metadata.get("langgraph_node") == "write" and isinstance(chunk, AIMessage):
                    text = message_text(chunk)
                    if text:
                        await ctx.events.emit(TOKEN, {"text": text})
        return await self._outcome(config)

    async def _starting_point(self, ctx: RunContext, config):
        snapshot = await self._graph.aget_state(config) if self._graph.checkpointer is not None else None
        if snapshot is not None and snapshot.values:
            pending = [i.value for t in snapshot.tasks for i in t.interrupts]
            if pending:
                if ctx.approval is None:
                    return ApprovalRequired(pending[0])  # 还没有答复（例如答复前被接管）：继续等
                log.info("resuming research after approval (approved=%s)", ctx.approval.get("approved"))
                return Command(resume=ctx.approval)
            if not snapshot.next:
                return self._reply(snapshot.values)  # 图已跑完，只是上一个执行者没来得及落库
            log.info("resuming research from checkpoint before %s", list(snapshot.next))
            return None
        history = ctx.history
        request = history[-1].content if history else ""
        background = "\n".join(f"{m.role}: {m.content}" for m in history[:-1])[-BACKGROUND_CHARS:]
        return {"request": request, "background": background, "findings": [], "usage": []}

    async def _outcome(self, config):
        snapshot = await self._graph.aget_state(config)
        pending = [i.value for t in snapshot.tasks for i in t.interrupts]
        if pending:
            return ApprovalRequired(pending[0])
        return self._reply(snapshot.values)

    @staticmethod
    def _reply(state: dict) -> AgentReply:
        usage = state.get("usage") or []
        log.info("llm usage: %d call(s), input=%d (cache_read=%d), output=%d", len(usage),
                 sum(u.get("input_tokens", 0) for u in usage),
                 sum((u.get("input_token_details") or {}).get("cache_read", 0) or 0 for u in usage),
                 sum(u.get("output_tokens", 0) for u in usage))
        attachment = state.get("attachment")
        return AgentReply(content=state.get("report") or "（没有生成报告）", attachments=[attachment] if attachment else None)
