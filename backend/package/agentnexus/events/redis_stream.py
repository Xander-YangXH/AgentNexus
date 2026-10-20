"""用 Redis Streams 传输运行事件：每个 run 一条流 `run:{run_id}:events`。

- 流 ID 单调递增，直接作为 SSE 的事件 id；客户端带 Last-Event-ID 重连时从该 id 之后继续读。
- XADD 带 MAXLEN ~ 防止单条流无限增长；终态事件写入时同时设置过期，run 结束后流自动清理。
- 发布失败只记日志、不抛错：事件只是视图，不能因为 Redis 抖动让 run 失败（数据库才是事实来源）。

Redis 客户端必须用 decode_responses=True 创建。
"""

import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis

from agentnexus.events.types import TERMINAL_EVENTS, EventSink, RunEvent

log = logging.getLogger(__name__)


def stream_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:events"


def make_reader_client(url: str, *, block_seconds: float, max_connections: int, **kwargs) -> Redis:
    """创建给阻塞读（XREAD BLOCK）用的 Redis 客户端。

    redis-py 8 的连接默认 socket_timeout=5 秒（from_url 不传时由连接类给默认值，已查证）。
    如果阻塞时长 >= 套接字超时，没有新事件时客户端会先于 Redis 超时，抛 TimeoutError 并断开连接。
    所以套接字超时必须明显大于阻塞时长。
    """
    return Redis.from_url(
        url,
        decode_responses=True,
        max_connections=max_connections,
        socket_timeout=block_seconds + 5,
        **kwargs,
    )


class RunEventPublisher:
    def __init__(self, redis: Redis, *, maxlen: int = 5000, ttl_seconds: int = 3600):
        self._redis = redis
        self._maxlen = maxlen
        self._ttl = ttl_seconds

    async def publish(self, run_id: uuid.UUID, type: str, data: dict[str, Any]) -> str | None:
        """写入一个事件，返回流 ID；失败返回 None（不抛错）。"""
        key = stream_key(run_id)
        try:
            # 终态事件和过期设置放进同一个 MULTI/EXEC：不会出现“写了终态但没设过期”的半截状态
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.xadd(
                    key,
                    {"type": type, "data": json.dumps(data, ensure_ascii=False)},
                    maxlen=self._maxlen,
                    approximate=True,
                )
                if type in TERMINAL_EVENTS:
                    pipe.expire(key, self._ttl)
                results = await pipe.execute()
            return results[0]
        except Exception:
            log.debug("publish %s for run %s failed", type, run_id, exc_info=True)
            return None

    def sink(self, run_id: uuid.UUID) -> EventSink:
        return _RunSink(self, run_id)


class _RunSink:
    """绑定到某个 run 的发布器。一个 run 会发几百个 token，Redis 不可用时只告警一次，避免刷屏。"""

    def __init__(self, publisher: RunEventPublisher, run_id: uuid.UUID):
        self._publisher = publisher
        self._run_id = run_id
        self._warned = False

    async def emit(self, type: str, data: dict[str, Any]) -> None:
        if await self._publisher.publish(self._run_id, type, data) is None and not self._warned:
            self._warned = True
            log.warning("events for run %s are not being delivered (redis unavailable?)", self._run_id)


class RunEventReader:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def read(self, run_id: uuid.UUID, after_id: str, block_ms: int | None) -> list[RunEvent]:
        """读取 after_id 之后的事件。block_ms=None 立即返回；否则最多阻塞这么久，有新事件立刻返回。

        注意：Redis 的 BLOCK 0 表示“永久阻塞”，所以这里从不传 0。
        """
        if block_ms is not None and block_ms <= 0:
            raise ValueError("block_ms must be positive or None")
        result = await self._redis.xread({stream_key(run_id): after_id}, block=block_ms, count=500)
        if not result:
            return []
        _key, entries = result[0]
        return [
            RunEvent(type=fields["type"], data=json.loads(fields["data"]), id=entry_id)
            for entry_id, fields in entries
        ]
