from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph

from agentnexus.agents.buildin.chatbot.context import ChatbotContext
from agentnexus.agents.buildin.chatbot.state import ChatState


def build_graph(context: ChatbotContext, checkpointer=None):
    async def call_model(state: ChatState):
        # 系统提示词只在调用模型时拼在最前面，不写进 state：
        # 这样 checkpoint 里不会重复保存它，改提示词也不需要迁移历史状态
        reply = await context.model.ainvoke([SystemMessage(context.system_prompt), *state["messages"]])
        return {"messages": [reply]}

    graph = StateGraph(ChatState)
    graph.add_node("model", call_model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    return graph.compile(checkpointer=checkpointer)
