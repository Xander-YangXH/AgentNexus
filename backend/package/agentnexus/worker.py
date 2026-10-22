"""worker 进程（decisions/0020）：执行对话 run、文档入库、图谱构建，并清理过期的 checkpoint。

用法：python -m agentnexus.worker（compose 的 worker 服务）。可以起多个副本：认领靠 PostgreSQL 租约（SKIP LOCKED），
不会重复执行；某个副本崩溃，它的任务在租约过期后由别的副本接管续做。

唤醒：订阅 PostgreSQL 的 NOTIFY（提交任务的事务里发出，内容是任务类型），收到后唤醒对应的执行器；
连接断了就重连，期间靠各执行器的轮询兜底。取消：订阅 Redis 的取消广播（runtime/cancel_bus.py）。
存活：每 10 秒更新一个文件的修改时间，compose 的健康检查据此判断进程是否卡死。
"""

import asyncio
import contextlib
import logging
import pathlib
import signal

import psycopg

from agentnexus import assembly
from agentnexus.config import get_settings
from agentnexus.observability.logging import configure_logging
from agentnexus.runtime.lease import NOTIFY_CHANNEL

log = logging.getLogger("agentnexus.worker")

ALIVE_FILE = pathlib.Path("/tmp/agentnexus-worker-alive")


async def listen_notifications(database_url: str, wake: dict) -> None:
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
                await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                log.info("listening for job notifications")
                # 不用 asyncio.wait_for(anext(...))：超时取消会把异步生成器关掉（选型实验踩到，decisions/0020）
                async for note in conn.notifies():
                    target = wake.get(note.payload)
                    if target is not None:
                        target()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("job notification listener lost; reconnecting in 2s (polling still works)", exc_info=True)
            await asyncio.sleep(2)


async def touch_alive() -> None:
    while True:
        ALIVE_FILE.touch()
        await asyncio.sleep(10)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    c = await assembly.build(settings, worker=True)
    wake = {"run": c.run_dispatcher.enqueue, "ingest": c.ingest_dispatcher.enqueue, "graph": c.graph_dispatcher.enqueue}
    await c.run_dispatcher.start()
    await c.ingest_dispatcher.start()
    await c.graph_dispatcher.start()
    c.janitor.start()
    background = [
        asyncio.create_task(listen_notifications(settings.database_url, wake)),
        asyncio.create_task(c.cancel_bus.listen(c.run_dispatcher.on_cancel_message)),
        asyncio.create_task(touch_alive()),
    ]
    log.info("worker started as %s", c.run_dispatcher.owner)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # 优雅关停：执行中的任务放弃租约（别的副本立即接手续做），而不是标为失败
    log.info("worker shutting down")
    for t in background:
        t.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await c.janitor.stop()
    await c.graph_dispatcher.stop()
    await c.ingest_dispatcher.stop()
    await c.run_dispatcher.stop()
    await assembly.close(c)
    with contextlib.suppress(FileNotFoundError):
        ALIVE_FILE.unlink()


if __name__ == "__main__":
    asyncio.run(main())
