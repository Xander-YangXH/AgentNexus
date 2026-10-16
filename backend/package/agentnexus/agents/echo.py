from agentnexus.agents.types import RunContext


async def echo_agent(ctx: RunContext) -> str:
    """占位 Agent：把最后一条用户消息原样返回。用来在没有 LLM 时打通“落库→派发→执行→回写”整条链路。"""
    last_user = next((m.content for m in reversed(ctx.history) if m.role == "user"), "")
    return f"echo: {last_user}"
