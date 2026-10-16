from agentnexus.agents.base import BaseAgent
from agentnexus.agents.buildin.chatbot.context import ChatbotContext
from agentnexus.agents.buildin.chatbot.graph import build_graph
from agentnexus.agents.messages import message_text, to_langchain
from agentnexus.agents.types import RunContext


class ChatbotAgent(BaseAgent):
    name = "chatbot"

    def __init__(self, context: ChatbotContext, checkpointer=None):
        self._graph = build_graph(context, checkpointer)

    async def run(self, ctx: RunContext) -> str:
        # 决策 0006：历史以 messages 表为准（ctx.history），整体喂给图；
        # checkpoint 的 thread_id 用 run_id，只保存这一次执行的进行中状态。
        result = await self._graph.ainvoke(
            {"messages": to_langchain(ctx.history)},
            config={"configurable": {"thread_id": str(ctx.run_id)}},
        )
        return message_text(result["messages"][-1])
