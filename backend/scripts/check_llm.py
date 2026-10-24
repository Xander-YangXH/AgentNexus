"""冒烟脚本：用当前 LLM_* 配置真实调用一次模型，不经过数据库和 HTTP。

用法（在仓库根目录，配置读自 .env）：
    docker compose run --rm --no-deps api python backend/scripts/check_llm.py
会打印：使用的 provider/model、非流式回复、流式的分块数量与回复长度、content 的原始类型。
不会打印 API key。
"""

import asyncio
import sys
import time

from agentnexus.config import get_settings
from agentnexus.models import build_chat_model
from agentnexus.models.factory import ModelConfigError
from agentnexus.agents.messages import message_text

PROMPT = "用一句话回答：1+1 等于几？只输出答案。"


def _usage(message) -> str:
    """输入 / 输出 / 其中思考 / 命中缓存的 token 数（各供应商字段不同，LangChain 统一到 usage_metadata）。"""
    u = message.usage_metadata or {}
    reasoning = (u.get("output_token_details") or {}).get("reasoning")
    cache_read = (u.get("input_token_details") or {}).get("cache_read")
    return f"输入 {u.get('input_tokens')} | 输出 {u.get('output_tokens')} | 其中思考 {reasoning} | 命中缓存 {cache_read}"
# 流式要用“长回答”才能判断是否逐段到达：一个字的回复生成瞬间完成，所有分块几乎同时到
LONG_PROMPT = "请用中文写一段约 300 字的介绍，主题是：为什么 AI 应用需要把对话历史存进数据库。"


async def main() -> int:
    settings = get_settings()
    print(f"provider={settings.llm_provider} model={settings.llm_model or '(fake)'} "
          f"base_url={settings.llm_base_url or '(default)'} key={'已设置' if settings.llm_api_key else '未设置'}")
    try:
        model = build_chat_model(settings)
    except ModelConfigError as exc:
        print(f"配置错误: {exc}")
        return 2

    t0 = time.perf_counter()
    reply = await model.ainvoke(PROMPT)
    print(f"[非流式] {time.perf_counter() - t0:.2f}s content类型={type(reply.content).__name__} 回复={message_text(reply)!r}")
    print(f"         用量 {_usage(reply)}")

    # 流式：思考模型会先输出一段“思考”块（不是正文），所以分开统计，首字时间按第一个“正文”块算
    for label, prompt in (("流式", PROMPT), ("流式·长文本", LONG_PROMPT)):
        t0 = time.perf_counter()
        first_text, text_arrivals, other_chunks, chars = None, [], 0, 0
        async for chunk in model.astream(prompt):
            now = time.perf_counter() - t0
            text = message_text(chunk)
            if text:
                first_text = first_text if first_text is not None else now
                text_arrivals.append(now)
                chars += len(text)
            else:
                other_chunks += 1  # 思考块、用量块、空的末块等
        if not text_arrivals:
            print(f"[{label}] 没有收到任何正文")
            continue
        spread = text_arrivals[-1] - text_arrivals[0]
        print(
            f"[{label}] 首个正文块 {first_text:.2f}s | 正文块 {len(text_arrivals)} | 非正文块 {other_chunks} "
            f"| 正文首末间隔 {spread:.2f}s | 字数 {chars}"
        )
        if label == "流式·长文本":
            ok = spread > 0.3 and len(text_arrivals) > 3
            print("判断：" + ("正文逐段到达（可做 token 级流式）" if ok else "正文几乎同时到达（不是有效的逐段流式）"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # 真实调用失败时给出类型和信息，便于判断是 key / 模型名 / 网络 / 额度
        print(f"调用失败: {type(exc).__name__}: {str(exc)[:500]}")
        sys.exit(1)
