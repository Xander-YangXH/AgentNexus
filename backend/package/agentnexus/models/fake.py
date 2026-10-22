import asyncio
import json
from collections.abc import AsyncIterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class FakeEchoChatModel(BaseChatModel):
    """确定性假模型：回复 `echo: <最后一条用户消息>`。

    用于没有密钥时跑通整条链路，也用于测试——把“链路 bug”和“LLM/提示词 bug”分开。
    支持流式：把回复切成小块逐个产出，用来测试 token 级的事件链路。
    支持工具（decisions/0018）：绑定了工具时，先用用户消息原文调用第一个工具；拿到工具结果后再回答，
    结果里有编号 [1] 时在回答末尾引用 [1]。这样不需要密钥也能演示和测试“检索 → 引用”的完整链路。
    """

    chunk_size: int = 2  # 每块字符数，模拟 token
    stream_delay: float = 0.0  # 每块之间的间隔（秒），测试“慢模型”时用
    bound_tools: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "fake-echo"

    def bind_tools(self, tools, **kwargs):
        names = tuple(t["function"]["name"] if "function" in t else t["name"] for t in tools)
        return self.model_copy(update={"bound_tools": names})

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        if self.bound_tools and isinstance(messages[-1], HumanMessage):
            calls = [{"name": self.bound_tools[0], "args": {"query": last}, "id": "call_0", "type": "tool_call"}]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        reply = f"echo: {last}"
        if isinstance(messages[-1], ToolMessage) and "[1]" in str(messages[-1].content):
            reply += " [1]"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        # 先走 _generate 得到完整回复，再切块：这样子类只要覆盖 _generate（如测试里记录调用、模拟失败），
        # 流式和非流式两条路径的行为就一致。
        # 这里不调用 run_manager.on_llm_new_token：LangGraph 的 token 流靠这个回调，但基类在遍历 _astream 时
        # 会自己调用它；而且基类调用 _astream 时并不传 run_manager（astream 与 ainvoke 两条路径都查过
        # langchain_core 源码），所以这里拿到的 run_manager 是 None，也不需要它。
        message = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        if message.tool_calls:  # 工具调用以一个块整体产出（真实模型会把参数分多块流出，合并后效果相同）
            chunks = [{"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
                      for i, c in enumerate(message.tool_calls)]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=chunks))
            return
        reply = message.content
        for i in range(0, len(reply), self.chunk_size):
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
            yield ChatGenerationChunk(message=AIMessageChunk(content=reply[i : i + self.chunk_size]))
