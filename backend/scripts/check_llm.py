"""冒烟脚本：用当前 LLM_* 配置真实调用一次模型，不经过数据库和 HTTP。

用法（在仓库根目录，配置读自 .env）：
    docker compose run --rm --no-deps api python backend/scripts/check_llm.py
会打印：使用的 provider/model、非流式回复、流式的分块数量与回复长度、content 的原始类型。
不会打印 API key。
"""

import asyncio
import os
import sys
import time

from agentnexus.config import get_settings
from agentnexus.models import build_chat_model
from agentnexus.models.factory import ModelConfigError
from agentnexus.agents.messages import message_text

PROMPT = "用一句话回答：1+1 等于几？只输出答案。"


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

    t0 = time.perf_counter()
    chunks, parts, first = 0, [], None
    async for chunk in model.astream(PROMPT):
        chunks += 1
        first = first if first is not None else time.perf_counter() - t0
        parts.append(message_text(chunk))
    text = "".join(parts)
    print(f"[流式]   {time.perf_counter() - t0:.2f}s 分块数={chunks} 首块延迟={first:.2f}s 回复={text!r}")
    if chunks <= 1 and settings.llm_provider != "fake":
        print("提示：只收到 1 个分块，说明这条路径目前并不是逐段流式（对 1.5 有影响，请告诉我）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # 真实调用失败时给出类型和信息，便于判断是 key / 模型名 / 网络 / 额度
        print(f"调用失败: {type(exc).__name__}: {str(exc)[:500]}")
        sys.exit(1)
