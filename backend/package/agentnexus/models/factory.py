"""LLM 供应商适配：业务代码只依赖 BaseChatModel，换供应商只改环境变量。"""

from langchain_core.language_models.chat_models import BaseChatModel

from agentnexus.config import Settings
from agentnexus.models.fake import FakeEchoChatModel


class ModelConfigError(ValueError):
    pass


_THINKING_VALUES = {"enabled", "disabled"}


def build_chat_model(settings: Settings) -> BaseChatModel:
    provider = settings.llm_provider.lower()
    thinking = settings.llm_thinking.lower() if settings.llm_thinking else None
    if thinking is not None and thinking not in _THINKING_VALUES:
        raise ModelConfigError(f"LLM_THINKING 只能是 enabled / disabled，或不设置（用供应商默认），收到 {settings.llm_thinking!r}")
    if thinking is not None and provider == "google":
        # Gemini 的思考配置是另一套参数（thinking budget），没接入：显式报错，避免“设置了却悄悄不生效”
        raise ModelConfigError("LLM_THINKING 目前只支持 openai / anthropic 分支")

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
            # DeepSeek 等兼容接口的思考开关不是 OpenAI 标准参数，只能放进 extra_body 透传
            extra = {"extra_body": {"thinking": {"type": thinking}}} if thinking else {}
            return ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                **extra,
            )
        if provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            # 显式传入 key：不依赖库自己去读 GOOGLE_API_KEY 之类的环境变量，配置来源只有 LLM_*
            return ChatGoogleGenerativeAI(model=settings.llm_model, api_key=settings.llm_api_key)
        from langchain_anthropic import ChatAnthropic

        # base_url 可指向任意 Anthropic 兼容接口（如 DeepSeek 的 https://api.deepseek.com/anthropic）；不传则用 Anthropic 官方地址
        # Anthropic 协议的 thinking 参数：开启时官方要求给 budget_tokens（DeepSeek 会忽略这个值）
        extra = {}
        if thinking == "enabled":
            extra["thinking"] = {"type": "enabled", "budget_tokens": 2048}
        elif thinking == "disabled":
            extra["thinking"] = {"type": "disabled"}
        return ChatAnthropic(
            model=settings.llm_model, api_key=settings.llm_api_key, base_url=settings.llm_base_url, **extra
        )

    raise ModelConfigError(f"未知的 LLM_PROVIDER: {settings.llm_provider!r}（可选 fake / openai / anthropic / google）")
