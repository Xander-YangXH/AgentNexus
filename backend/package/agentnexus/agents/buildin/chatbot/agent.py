import logging
import re

from langchain_core.messages import AIMessage, ToolMessage

from agentnexus.agents.base import BaseAgent
from agentnexus.agents.buildin.chatbot.context import ChatbotContext
from agentnexus.agents.buildin.chatbot.graph import build_graph
from agentnexus.agents.buildin.chatbot.tools import mark_cited
from agentnexus.agents.messages import message_text, to_langchain
from agentnexus.agents.types import AgentReply, RunContext
from agentnexus.events import TOKEN, TOOL_FINISHED, TOOL_STARTED

_SOURCE_LINE = re.compile(r"^\[\d+\] 来源：", re.M)
log = logging.getLogger(__name__)


class ChatbotAgent(BaseAgent):
    name = "chatbot"

    def __init__(self, context: ChatbotContext, checkpointer=None):
        self._context = context
        self._graph = build_graph(context, checkpointer)

    async def run(self, ctx: RunContext) -> AgentReply:
        # 决策 0006：历史以 messages 表为准（ctx.history），整体喂给图；
        # checkpoint 的 thread_id 用 run_id，只保存这一次执行的进行中状态。
        # 决策 0018：检索哪些知识库、以谁的身份检索，放在 configurable 里传给工具节点（模型看不到、改不了）。
        config = {
            "configurable": {
                "thread_id": str(ctx.run_id),
                "user_id": str(ctx.user_id) if ctx.user_id else None,
                "knowledge_base_ids": [str(k) for k in ctx.knowledge_base_ids],
            },
            # 兜底：模型无视“检索次数已达上限”反复调用工具时，图在这么多步后报错，不会无限循环、无限花钱
            "recursion_limit": 2 * (self._context.max_tool_rounds + 2) + 1,
        }
        # 决策 0007：同时订阅三种流——
        #   "messages"：模型逐段产出的 token；
        #   "updates" ：每个节点的输出，用来发工具事件（开始检索 / 检索完成），让前端显示“正在检索”；
        #   "values"  ：每一步之后的完整状态，最后一个就是最终状态。
        # 最终回复取自最终状态，而不是把 token 拼起来：状态才是图认定的结果，token 只是过程中的视图。
        final_state = None
        graph_input = {"messages": to_langchain(ctx.history), "citations": []}
        if ctx.attempt > 1 and self._graph.checkpointer is not None:
            # 接管（decisions/0020）：thread_id 就是 run_id，上一个 worker 的进度都在 checkpoint 里
            snapshot = await self._graph.aget_state(config)
            if snapshot.values.get("messages"):
                if not snapshot.next:
                    # 图已经跑完，只是上一个 worker 没来得及把结果写进数据库：直接用最终状态，不再调用模型
                    log.info("resuming run from a finished checkpoint")
                    return self._reply(snapshot.values)
                # 从最后一个 checkpoint 继续：已完成的节点（检索、模型调用）不重做，只重做被打断的那一步
                log.info("resuming run from checkpoint before %s", list(snapshot.next))
                graph_input = None
        async for mode, payload in self._graph.astream(
            graph_input,
            config,
            stream_mode=["messages", "updates", "values"],
        ):
            if mode == "messages":
                chunk, _metadata = payload
                if isinstance(chunk, AIMessage):  # AIMessageChunk 是 AIMessage 的子类
                    text = message_text(chunk)
                    if text:  # 跳过空块（如模型流结束时基类补发的空末块、只有工具调用的块）
                        await ctx.events.emit(TOKEN, {"text": text})
            elif mode == "updates":
                await self._emit_tool_events(ctx, payload)
            else:
                final_state = payload
        self._log_usage(final_state["messages"][len(ctx.history):])
        return self._reply(final_state)

    @staticmethod
    def _reply(state: dict) -> AgentReply:
        content = message_text(state["messages"][-1])
        citations = state.get("citations") or []
        return AgentReply(content=content, citations=mark_cited(citations, content) if citations else None)

    @staticmethod
    def _log_usage(new_messages) -> None:
        """本次执行所有模型调用的 token 用量合计（一次回答可能调用模型多次：检索前一次、回答一次）。
        先写日志（run_id 由上下文带上），阶段 5 的成本看板再落库统计。"""
        calls = [m for m in new_messages if isinstance(m, AIMessage) and m.usage_metadata]
        if not calls:
            return
        total = {"input": 0, "output": 0, "cache_read": 0}
        for m in calls:
            u = m.usage_metadata
            total["input"] += u.get("input_tokens", 0)
            total["output"] += u.get("output_tokens", 0)
            total["cache_read"] += (u.get("input_token_details") or {}).get("cache_read", 0) or 0
        log.info("llm usage: %d call(s), input=%d (cache_read=%d), output=%d",
                 len(calls), total["input"], total["cache_read"], total["output"],
                 extra={"llm_calls": len(calls), **{f"{k}_tokens": v for k, v in total.items()}})

    async def _emit_tool_events(self, ctx: RunContext, update: dict) -> None:
        # 模型节点产出工具调用、工具节点产出工具结果；预检索节点一次产出两者（decisions/0018）
        for output in update.values():
            for msg in (output or {}).get("messages", []):
                if isinstance(msg, AIMessage):
                    for call in msg.tool_calls:
                        # 模型在调用工具前可能先说了几句（“我查一下”），那段文字不会成为最终回复；
                        # 前端收到 tool.started 时清空正在显示的临时文字（与“最终回复取自最终状态”保持一致）
                        await ctx.events.emit(TOOL_STARTED, {"name": call["name"], "query": str(call["args"].get("query", ""))})
                elif isinstance(msg, ToolMessage):
                    hits = len(_SOURCE_LINE.findall(msg.content)) if isinstance(msg.content, str) else 0
                    await ctx.events.emit(TOOL_FINISHED, {"name": msg.name or "knowledge_search", "hits": hits,
                                                          "ok": msg.status != "error"})
