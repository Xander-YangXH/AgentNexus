from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel

from agentnexus.agents.base import BaseContext
from agentnexus.agents.buildin.chatbot.prompt import DEFAULT_SYSTEM_PROMPT, KNOWLEDGE_PROMPT, PREFETCH_PROMPT
from agentnexus.agents.buildin.chatbot.tools import KnowledgeSearch


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
