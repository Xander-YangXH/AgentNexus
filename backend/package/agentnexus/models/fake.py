import asyncio
from collections.abc import AsyncIterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class FakeEchoChatModel(BaseChatModel):
    """确定性假模型：回复 `echo: <最后一条用户消息>`。

    用于没有密钥时跑通整条链路，也用于测试——把“链路 bug”和“LLM/提示词 bug”分开。
    支持流式：把回复切成小块逐个产出，用来测试 token 级的事件链路。
    """

    chunk_size: int = 2  # 每块字符数，模拟 token
    stream_delay: float = 0.0  # 每块之间的间隔（秒），测试“慢模型”时用

    @property
    def _llm_type(self) -> str:
        return "fake-echo"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"echo: {last}"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        # 先走 _generate 得到完整回复，再切块：这样子类只要覆盖 _generate（如测试里记录调用、模拟失败），
        # 流式和非流式两条路径的行为就一致。
        # 这里不调用 run_manager.on_llm_new_token：LangGraph 的 token 流靠这个回调，但基类在遍历 _astream 时
        # 会自己调用它；而且基类调用 _astream 时并不传 run_manager（astream 与 ainvoke 两条路径都查过
        # langchain_core 源码），所以这里拿到的 run_manager 是 None，也不需要它。
        reply = self._generate(messages, stop, run_manager, **kwargs).generations[0].message.content
        for i in range(0, len(reply), self.chunk_size):
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
            yield ChatGenerationChunk(message=AIMessageChunk(content=reply[i : i + self.chunk_size]))
