"""跨进程取消广播（decisions/0020）：Redis Pub/Sub。

只用来“加速”：取消请求的事实来源是数据库里的 agent_runs.cancel_requested_at，执行它的 worker 心跳时也会检查。
所以这里发丢了（Redis 抖动、worker 恰好在重连）也只是取消晚一个心跳间隔，不会丢失。
Pub/Sub 不保存历史，正适合这种“只对此刻在线的进程有意义”的信号；需要可靠送达的事件走 Redis Streams（0007）。
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable

from redis.asyncio import Redis

log = logging.getLogger(__name__)

CHANNEL = "agentnexus:run-cancel"


class RedisCancelBus:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def publish(self, run_id: uuid.UUID) -> None:
        try:
            await self._redis.publish(CHANNEL, str(run_id))
        except Exception:
            log.warning("cancel broadcast failed for run %s (worker will see it at next heartbeat)", run_id, exc_info=True)

    async def listen(self, on_cancel: Callable[[uuid.UUID], None]) -> None:
        """一直订阅，断线后重连。由 worker 进程作为后台任务运行。"""
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(CHANNEL)
                    async for message in pubsub.listen():
                        if message.get("type") == "message":
                            with contextlib.suppress(ValueError):  # 格式不对的消息直接忽略
                                on_cancel(uuid.UUID(message["data"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("cancel subscription lost; reconnecting", exc_info=True)
                await asyncio.sleep(1)
