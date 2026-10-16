from langgraph.graph import MessagesState

# 当前只需要消息列表；后续需要额外状态（如检索结果、计划）时在这里扩展
ChatState = MessagesState
