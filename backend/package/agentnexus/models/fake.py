from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeEchoChatModel(BaseChatModel):
    """确定性假模型：回复 `echo: <最后一条用户消息>`。

    用于没有密钥时跑通整条链路，也用于测试——把“链路 bug”和“LLM/提示词 bug”分开。
    """

    @property
    def _llm_type(self) -> str:
        return "fake-echo"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"echo: {last}"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)
