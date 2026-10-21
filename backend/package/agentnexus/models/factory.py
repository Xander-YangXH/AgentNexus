"""LLM 供应商适配：业务代码只依赖 BaseChatModel，换供应商只改环境变量。"""

from langchain_core.language_models.chat_models import BaseChatModel

from agentnexus.config import Settings
from agentnexus.models.fake import FakeEchoChatModel


class ModelConfigError(ValueError):
    pass


def build_chat_model(settings: Settings) -> BaseChatModel:
    provider = settings.llm_provider.lower()

    if provider == "fake":
        return FakeEchoChatModel(stream_delay=settings.llm_fake_stream_delay)

    if provider in ("openai", "anthropic", "google"):
        if not settings.llm_model:
            raise ModelConfigError(f"LLM_PROVIDER={provider} 需要设置 LLM_MODEL")
        if not settings.llm_api_key:
            raise ModelConfigError(f"LLM_PROVIDER={provider} 需要设置 LLM_API_KEY")
        # 延迟导入：用 fake 时不加载供应商 SDK，启动更快
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            # base_url 指向任意 OpenAI 兼容接口（DeepSeek、通义、智谱等）
            return ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
            )
        if provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            # 显式传入 key：不依赖库自己去读 GOOGLE_API_KEY 之类的环境变量，配置来源只有 LLM_*
            return ChatGoogleGenerativeAI(model=settings.llm_model, api_key=settings.llm_api_key)
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=settings.llm_model, api_key=settings.llm_api_key)

    raise ModelConfigError(f"未知的 LLM_PROVIDER: {settings.llm_provider!r}（可选 fake / openai / anthropic / google）")
