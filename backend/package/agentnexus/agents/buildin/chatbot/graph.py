import logging
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agentnexus.agents.buildin.chatbot.context import ChatbotContext
from agentnexus.agents.buildin.chatbot.state import ChatState
from agentnexus.agents.messages import message_text
from agentnexus.agents.buildin.chatbot.tools import (
    KNOWLEDGE_SEARCH,
    KNOWLEDGE_SEARCH_TOOL,
    MAX_QUERY_CHARS,
    format_for_model,
    to_citation,
)

log = logging.getLogger(__name__)

ROUND_LIMIT_REACHED = "本次回答的检索次数已达上限，请根据已经检索到的内容回答。"


def _kb_ids(config: RunnableConfig) -> list[uuid.UUID]:
    return [uuid.UUID(k) for k in config["configurable"].get("knowledge_base_ids", [])]


def _tool_rounds(messages) -> int:
    """本轮（最后一条用户消息之后）模型已经发起过几次工具调用。"""
    rounds = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, AIMessage) and m.tool_calls:
            rounds += 1
    return rounds


def build_graph(context: ChatbotContext, checkpointer=None):
    async def call_model(state: ChatState, config: RunnableConfig):
        # 系统提示词只在调用模型时拼在最前面，不写进 state：
        # 这样 checkpoint 里不会重复保存它，改提示词也不需要迁移历史状态
        model, prompt = context.model, context.system_prompt
        if context.knowledge_search is not None and _kb_ids(config):
            # 只有对话关联了知识库才提供工具。达到轮数上限后仍然绑定工具：消息里已有工具调用时，
            # Anthropic 格式要求请求里定义工具；上限改由工具节点直接拒绝执行来保证
            model = model.bind_tools([KNOWLEDGE_SEARCH_TOOL])
            prompt = f"{prompt}\n\n{context.knowledge_prompt}"
            if context.prefetch:
                prompt = f"{prompt}\n{context.prefetch_prompt}"
        reply = await model.ainvoke([SystemMessage(prompt), *state["messages"]])
        return {"messages": [reply]}

    async def run_calls(tool_calls, state: ChatState, config: RunnableConfig, *, over_limit: bool):
        """执行一组工具调用，返回（工具结果消息, 新增的引用）。模型发起的调用和预检索共用这一段。"""
        known = {c["chunk_id"]: c["n"] for c in state.get("citations", [])}  # 同一片段多次检索到时沿用原编号
        next_n = len(known) + 1
        replies, new_citations = [], []
        for call in tool_calls:
            def reply(text, **kw):
                return ToolMessage(text, tool_call_id=call["id"], name=call["name"], **kw)

            if call["name"] != KNOWLEDGE_SEARCH:
                replies.append(reply(f"未知工具：{call['name']}", status="error"))
                continue
            if over_limit:
                replies.append(reply(ROUND_LIMIT_REACHED, status="error"))
                continue
            query = str(call["args"].get("query", "")).strip()[:MAX_QUERY_CHARS]
            if not query:
                replies.append(reply("query 不能为空", status="error"))
                continue
            try:
                user_id = uuid.UUID(config["configurable"]["user_id"])
                hits = await context.knowledge_search(user_id, _kb_ids(config), query)
            except Exception:
                # 检索失败不让整个回答失败：告诉模型检索不可用，由它如实告知用户（提示词要求不编造）
                log.warning("knowledge_search failed", exc_info=True)
                replies.append(reply("知识库检索暂时不可用。", status="error"))
                continue
            numbered = []
            for hit in hits:
                key = str(hit.chunk_id)
                if key not in known:
                    known[key] = next_n
                    new_citations.append(to_citation(next_n, hit))
                    next_n += 1
                numbered.append((known[key], hit))
            replies.append(reply(format_for_model(numbered)))
        return replies, new_citations

    async def call_tools(state: ChatState, config: RunnableConfig):
        over_limit = _tool_rounds(state["messages"]) > context.max_tool_rounds
        replies, new_citations = await run_calls(state["messages"][-1].tool_calls, state, config, over_limit=over_limit)
        return {"messages": replies, "citations": new_citations}

    async def prefetch(state: ChatState, config: RunnableConfig):
        """预检索（decisions/0018）：调用模型之前，先用用户的原话检索一次，结果以“工具调用 + 工具结果”的形式放进上下文。

        为什么：只让模型自己决定时，评测里约 3% 的问题模型没检索、直接反问用户；预检索保证每个问题都有据可查，
        而且模型拿到结果后通常一次就能回答，比“模型决定检索 → 再回答”少一次模型调用。
        原话不适合当检索词时（如追问“那定制的呢”），模型仍可以用补全后的检索词再调用工具。
        """
        last = state["messages"][-1]
        if not (context.prefetch and context.knowledge_search and _kb_ids(config) and isinstance(last, HumanMessage)):
            return None
        query = message_text(last).strip()[:MAX_QUERY_CHARS]
        if not query:
            return None
        call = {"name": KNOWLEDGE_SEARCH, "args": {"query": query}, "id": f"prefetch_{uuid.uuid4().hex[:12]}", "type": "tool_call"}
        replies, new_citations = await run_calls([call], state, config, over_limit=False)
        return {"messages": [AIMessage(content="", tool_calls=[call]), *replies], "citations": new_citations}

    def route(state: ChatState):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(ChatState)
    graph.add_node("prefetch", prefetch)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    graph.add_edge(START, "prefetch")
    graph.add_edge("prefetch", "model")
    graph.add_conditional_edges("model", route, ["tools", END])
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)
