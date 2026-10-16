"""LangGraph checkpoint 的连接池与建表。

checkpoint 的表结构由 LangGraph 自己管理（`setup()`），不放进我们的 Alembic，
避免两边各维护一份、升级时互相打架。`python -m agentnexus.storage.checkpoint` 供 migrate 服务调用。
"""

import asyncio
import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def open_pool(database_url: str, *, max_size: int = 10) -> AsyncConnectionPool:
    # autocommit / prepare_threshold=0 / dict_row 是 AsyncPostgresSaver 对连接的要求
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,  # psycopg_pool 的 min_size 默认是 4，max_size 比它小会直接报错
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    return pool


async def setup_checkpoint_tables(database_url: str) -> None:
    pool = await open_pool(database_url, max_size=1)
    try:
        await AsyncPostgresSaver(pool).setup()  # 幂等
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(setup_checkpoint_tables(os.environ["DATABASE_URL"]))
    print("checkpoint tables ready")
