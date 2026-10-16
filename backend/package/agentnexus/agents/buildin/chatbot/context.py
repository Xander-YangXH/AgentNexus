from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel

from agentnexus.agents.base import BaseContext
from agentnexus.agents.buildin.chatbot.prompt import DEFAULT_SYSTEM_PROMPT


@dataclass
class ChatbotContext(BaseContext):
    model: BaseChatModel
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
