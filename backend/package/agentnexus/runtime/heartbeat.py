"""worker 进程的心跳（阶段 7.8，decisions/0031）。

租约只能说明“谁在执行任务”，空闲的 worker 和已经死掉的 worker 都看不到。每个 worker 每 10 秒写一次
worker_heartbeats，运行状态把 30 秒内有心跳的算作在线。正常退出时删除自己的行；崩溃留下的行超过一小时由
任何一个在线 worker 顺手清掉。
"""

import asyncio
import logging

from sqlalchemy import delete, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.storage.postgres import WorkerHeartbeat

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 10.0
PRUNE_AFTER = "1 hour"


async def beat(sessionmaker: async_sessionmaker, owner: str) -> None:
    stmt = insert(WorkerHeartbeat).values(owner=owner).on_conflict_do_update(
        index_elements=[WorkerHeartbeat.owner], set_={"last_seen_at": func.now()})
    async with sessionmaker() as session, session.begin():
        await session.execute(stmt)
        await session.execute(delete(WorkerHeartbeat).where(
            WorkerHeartbeat.last_seen_at < func.now() - text(f"interval '{PRUNE_AFTER}'")))


async def forget(sessionmaker: async_sessionmaker, owner: str) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.owner == owner))


async def run(sessionmaker: async_sessionmaker, owner: str) -> None:
    while True:
        try:
            await beat(sessionmaker, owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("writing worker heartbeat failed", exc_info=True)  # 数据库短暂不可用：下一次再写
        await asyncio.sleep(INTERVAL_SECONDS)
