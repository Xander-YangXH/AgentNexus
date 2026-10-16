from abc import ABC, abstractmethod
from dataclasses import dataclass

from agentnexus.agents.types import RunContext


@dataclass
class BaseContext:
    """Agent 的运行期依赖（模型、工具、提示词等）。与 RunContext 不同：
    RunContext 是“这一次执行的输入”，BaseContext 是“这个 Agent 长期持有的装配”。"""


class BaseAgent(ABC):
    name: str

    @abstractmethod
    async def run(self, ctx: RunContext) -> str:
        """给定一次执行的输入，返回助手回复文本。可直接作为 RunDispatcher 的 handler。"""
