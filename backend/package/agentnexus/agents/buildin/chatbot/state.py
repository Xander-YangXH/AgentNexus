import operator
from typing import Annotated

from langgraph.graph import MessagesState


def _union(old: list[str], new: list[str]) -> list[str]:
    return old + [x for x in new if x not in old]


class ChatState(MessagesState):
    # 本次执行里检索到的片段（按编号 n 追加）。每次执行用新的 thread（run_id），所以从空开始（decisions/0006、0018）
    citations: Annotated[list[dict], operator.add]
    # 本次执行里已加载的技能（decisions/0021）：只增不减，决定这次回答可以用哪些工具
    skills: Annotated[list[str], _union]
    # 本次执行里交给用户的文件（share_file，阶段 7.2）：放在状态里随 checkpoint 保存，接管后不丢
    attachments: Annotated[list[dict], operator.add]
