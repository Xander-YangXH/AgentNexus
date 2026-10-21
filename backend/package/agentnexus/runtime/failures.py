"""把执行失败归类成稳定的错误码（decisions/0009）。

原始异常文本只写进 agent_runs.error 和日志，供开发者排查；对外（API、事件、前端）只给错误码，
由前端翻译成面向用户的提示。原因：原始报错可能包含供应商的内部信息、请求细节，且对用户没有意义。

识别方式：沿异常链（__cause__ / __context__）找 HTTP 状态码。
- OpenAI、Anthropic 的 SDK 异常带 status_code；
- Gemini：LangChain 包装出来的异常本身不带状态码，但异常链上 Google SDK 的异常带 code（如 400）和
  status（如 "INVALID_ARGUMENT"）——已在容器里用错误的 key 实际触发验证。
这是启发式规则：找不到状态码一律归为 internal，宁可归类保守，也不错把内部错误说成“供应商问题”。
"""

import enum
from collections.abc import Iterator


class ErrorCode(enum.StrEnum):
    TIMEOUT = "timeout"  # 超过 RUN_TIMEOUT_SECONDS
    LLM_RATE_LIMITED = "llm_rate_limited"  # 429：请求太频繁或额度用完
    LLM_UNAVAILABLE = "llm_unavailable"  # 5xx：供应商过载或故障（如 Gemini 503 high demand）
    LLM_CONFIG = "llm_config"  # 400/401/403/404：密钥、模型名等配置问题
    INTERRUPTED = "interrupted"  # 服务关停或重启打断了执行
    INTERNAL = "internal"  # 其他：多半是我们自己的 bug


_STATUS_TEXT = {"RESOURCE_EXHAUSTED": 429, "UNAVAILABLE": 503}


def _chain(exc: BaseException | None, limit: int = 10) -> Iterator[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < limit:  # 防止异常链成环
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _http_status(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        # 只认 100~599 的整数：很多异常也有 code 属性（如字符串错误码），不能误认
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    status = getattr(exc, "status", None)
    return _STATUS_TEXT.get(status) if isinstance(status, str) else None


def classify(exc: BaseException) -> ErrorCode:
    for e in _chain(exc):
        status = _http_status(e)
        if status is None:
            continue
        if status == 429:
            return ErrorCode.LLM_RATE_LIMITED
        if status >= 500:
            return ErrorCode.LLM_UNAVAILABLE
        if status in (400, 401, 403, 404):
            return ErrorCode.LLM_CONFIG
    return ErrorCode.INTERNAL
