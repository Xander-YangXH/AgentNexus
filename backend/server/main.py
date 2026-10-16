import asyncio

import psycopg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException

from agentnexus.config import get_settings

app = FastAPI(title="AgentNexus")

CHECK_TIMEOUT = 3  # 秒；依赖不可达时让探针快速失败，而不是挂住


@app.get("/health")
async def health() -> dict:
    """存活探针：进程能响应即 200，不查依赖（依赖挂了重启 api 也没用）。"""
    return {"status": "ok"}


async def _check_postgres(url: str) -> None:
    async with await psycopg.AsyncConnection.connect(url, connect_timeout=CHECK_TIMEOUT) as conn:
        await conn.execute("SELECT 1")


async def _check_redis(url: str) -> None:
    client = aioredis.from_url(url, socket_connect_timeout=CHECK_TIMEOUT)
    try:
        await client.ping()
    finally:
        await client.aclose()


@app.get("/health/ready")
async def ready() -> dict:
    """就绪探针：真去连 Postgres 和 Redis，任一失败返回 503。"""
    settings = get_settings()
    checks = {
        "postgres": _check_postgres(settings.database_url),
        "redis": _check_redis(settings.redis_url),
    }
    results = await asyncio.gather(*checks.values(), return_exceptions=True)
    status = {
        name: "ok" if not isinstance(res, BaseException) else f"error: {type(res).__name__}"
        for name, res in zip(checks, results)
    }
    if any(v != "ok" for v in status.values()):
        raise HTTPException(status_code=503, detail=status)
    return status
