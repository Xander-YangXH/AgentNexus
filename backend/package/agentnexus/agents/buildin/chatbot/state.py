import operator
from typing import Annotated

from langgraph.graph import MessagesState


class ChatState(MessagesState):
    # 本次执行里检索到的片段（按编号 n 追加）。每次执行用新的 thread（run_id），所以从空开始（decisions/0006、0018）
    citations: Annotated[list[dict], operator.add]
