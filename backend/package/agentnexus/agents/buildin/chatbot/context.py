from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel

from agentnexus.agents.base import BaseContext
from agentnexus.agents.buildin.chatbot.prompt import DEFAULT_SYSTEM_PROMPT, KNOWLEDGE_PROMPT, PREFETCH_PROMPT, SKILLS_PROMPT
from agentnexus.agents.buildin.chatbot.tools import KnowledgeSearch
from agentnexus.agents.skills import Skill
from agentnexus.agents.tools import ToolSource
from agentnexus.services.usage import UsageRecorder


@dataclass
class ChatbotContext(BaseContext):
    model: BaseChatModel
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # 知识库检索。为 None 时（例如测试、或没有部署检索）即使对话关联了知识库也不提供工具
    knowledge_search: KnowledgeSearch | None = None
    knowledge_prompt: str = KNOWLEDGE_PROMPT
    max_tool_rounds: int = 3  # 一次回答最多检索几轮（含预检索），防止模型反复检索、成本失控
    prefetch: bool = False  # 调用模型前先用用户原话检索一次（graph.py 的 prefetch；部署默认值见 settings）
    prefetch_prompt: str = PREFETCH_PROMPT
    # 通用工具（decisions/0021）：沙盒、MCP 等。只有被已加载的技能列出的工具才会提供给模型
    tool_sources: Sequence[ToolSource] = ()
    skills: dict[str, Skill] = field(default_factory=dict)
    skills_prompt: str = SKILLS_PROMPT
    # 加载了技能之后的工具轮数上限：数据分析要“写文件 → 执行 → 看报错 → 改 → 再执行”，3 轮不够
    max_skill_tool_rounds: int = 8
    # 用量采集（decisions/0023）：每次模型调用结束写一行明细。None：不记（测试）
    usage: "UsageRecorder | None" = None
