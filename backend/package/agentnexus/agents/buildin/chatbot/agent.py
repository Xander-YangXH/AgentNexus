from langchain_core.messages import AIMessage

from agentnexus.agents.base import BaseAgent
from agentnexus.agents.buildin.chatbot.context import ChatbotContext
from agentnexus.agents.buildin.chatbot.graph import build_graph
from agentnexus.agents.messages import message_text, to_langchain
from agentnexus.agents.types import RunContext
from agentnexus.events import TOKEN


class ChatbotAgent(BaseAgent):
    name = "chatbot"

    def __init__(self, context: ChatbotContext, checkpointer=None):
        self._graph = build_graph(context, checkpointer)

    async def run(self, ctx: RunContext) -> str:
        # 决策 0006：历史以 messages 表为准（ctx.history），整体喂给图；
        # checkpoint 的 thread_id 用 run_id，只保存这一次执行的进行中状态。
        # 决策 0007：同时订阅两种流——
        #   "messages"：模型逐段产出的 token（节点里用的是 ainvoke，LangGraph 通过回调拿到逐段输出），
        #   "values"  ：每一步之后的完整状态，最后一个就是最终状态。
        # 最终回复取自最终状态，而不是把 token 拼起来：状态才是图认定的结果，token 只是过程中的视图。
        config = {"configurable": {"thread_id": str(ctx.run_id)}}
        final_state = None
        async for mode, payload in self._graph.astream(
            {"messages": to_langchain(ctx.history)}, config, stream_mode=["messages", "values"]
        ):
            if mode == "messages":
                chunk, _metadata = payload
                if isinstance(chunk, AIMessage):  # AIMessageChunk 是 AIMessage 的子类
                    text = message_text(chunk)
                    if text:  # 跳过空块（如模型流结束时基类补发的空末块）
                        await ctx.events.emit(TOKEN, {"text": text})
            else:
                final_state = payload
        return message_text(final_state["messages"][-1])
