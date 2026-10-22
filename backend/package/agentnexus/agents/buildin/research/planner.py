"""调研计划（decisions/0022）：结构化输出，子任务数量有上限——审批时用户看到的就是要执行的东西。"""

import dataclasses
import re
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agentnexus.agents.buildin.research.prompt import PLANNER_PROMPT


class Subtask(BaseModel):
    title: str = Field(description="子任务标题，不超过 20 字")
    question: str = Field(description="执行者要回答的具体问题，自包含")


class Plan(BaseModel):
    subtasks: list[Subtask] = Field(description="2~5 个可以并行完成的子任务")


@dataclasses.dataclass
class PlanResult:
    subtasks: list[dict]
    usage: dict


class Planner(Protocol):
    async def __call__(self, request: str, feedback: str | None, previous: list[dict] | None) -> PlanResult: ...


def _prompt(request: str, feedback: str | None, previous: list[dict] | None) -> str:
    text = f"调研任务：{request}"
    if previous and feedback:
        lines = "\n".join(f"{i + 1}. {s['title']}：{s['question']}" for i, s in enumerate(previous))
        text += f"\n\n上一版计划：\n{lines}\n\n用户的修改意见：{feedback}"
    return text


class LLMPlanner:
    def __init__(self, model: BaseChatModel, *, max_subtasks: int = 5):
        # 结构化输出走强制工具调用，与思考模式不兼容：装配时传入关闭思考的模型（同 0019 的图谱抽取）
        self._structured = model.with_structured_output(Plan, include_raw=True)
        self._max = max_subtasks

    async def __call__(self, request, feedback, previous) -> PlanResult:
        res = await self._structured.ainvoke([SystemMessage(PLANNER_PROMPT.format(max_subtasks=self._max)),
                                              HumanMessage(_prompt(request, feedback, previous))])
        if res.get("parsed") is None:
            raise ValueError(f"planner returned no valid plan: {res.get('parsing_error')}")
        subtasks = [{"title": s.title.strip()[:40], "question": s.question.strip()[:500]}
                    for s in res["parsed"].subtasks if s.title.strip() and s.question.strip()]
        if not subtasks:
            raise ValueError("planner returned an empty plan")
        return PlanResult(subtasks[: self._max], dict(res["raw"].usage_metadata or {}))


class FakePlanner:
    """假模型部署用（LLM_PROVIDER=fake）：按“、，;；和换行”切分任务，不调用模型。"""

    def __init__(self, *, max_subtasks: int = 5):
        self._max = max_subtasks

    async def __call__(self, request, feedback, previous) -> PlanResult:
        parts = [p.strip() for p in re.split(r"[、，,;；\n]|和", request) if p.strip()] or [request]
        if feedback and previous:
            parts = parts[: max(1, len(previous) - 1)]  # 有修改意见时少一个子任务，便于演示“改了计划”
        return PlanResult([{"title": p[:20], "question": f"关于“{p}”有哪些要点？"} for p in parts[: self._max]], {})
