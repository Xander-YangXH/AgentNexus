"""登录限流（decisions/0025）：按“账号 + IP”和按 IP 计数，超过次数返回 429。

实验（eval/security_exp/bruteforce.py）：没有限流时，对一个真实账号每秒能试 217 次密码。argon2id 防的是
“库泄露后离线破解”，挡不住在线猜测——服务端会用线程池并发地算。

- 不按“账号”单独锁：那样攻击者用错误密码就能把任何人锁在外面。按“账号 + IP”，攻击者只能锁住自己；
- 按 IP 再加一层宽松的上限：挡住“一个 IP 轮流猜很多账号”；
- 先计数、再校验密码（INCR 是原子的）：并发的请求各自拿到不同的计数，不会出现“20 个请求同时通过检查”；
  登录成功再把这次退回去（清掉“账号 + IP”的计数，IP 计数减一）。只在登录成功时清零“账号 + IP”，
  不清 IP 计数：否则攻击者用自己的账号登录一次就能把 IP 计数清零；
- 固定窗口：第一次失败开始计时，窗口到期整组计数一起过期（EXPIRE NX）；
- 计不了数时拒绝登录（503），不放行。第一版是“Redis 出错就放行”，compose 实测时 20 个并发请求把连接池
  （上限 10）占满，超出的请求拿不到连接、报错、被放行——攻击者只要并发发请求就能绕过限流。现在两处改动：
  连接池满了排队等（BlockingConnectionPool），真的计不了数就拒绝。安全检查出错时不能默认通过。
"""

import dataclasses
import hashlib
import logging

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class LoginThrottleUnavailable(Exception):
    """计不了数（Redis 不可用）：拒绝登录，不放行。"""


class TooManyLoginAttempts(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"too many login attempts, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclasses.dataclass(frozen=True)
class _Keys:
    pair: str
    ip: str


class LoginThrottle:
    def __init__(self, redis: Redis, *, pair_limit: int = 5, ip_limit: int = 30, window_seconds: int = 900,
                 prefix: str = "login"):
        self.redis = redis
        self.pair_limit = pair_limit
        self.ip_limit = ip_limit
        self.window = window_seconds
        self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, *, max_connections: int = 20, pool_timeout: float = 5.0, **kw) -> "LoginThrottle":
        # 连接池满了就排队等，而不是立刻报错（普通 ConnectionPool 满了抛 MaxConnectionsError）
        pool = BlockingConnectionPool.from_url(url, max_connections=max_connections, timeout=pool_timeout,
                                               decode_responses=True)
        return cls(Redis.from_pool(pool), **kw)

    async def close(self) -> None:
        await self.redis.aclose()

    def _keys(self, name: str, ip: str) -> _Keys:
        # 用户名取哈希再进键名：Redis 里不留明文用户名，长度也固定
        pair = hashlib.sha256(f"{name}\0{ip}".encode()).hexdigest()
        return _Keys(f"{self.prefix}:pair:{pair}", f"{self.prefix}:ip:{ip}")

    async def attempt(self, name: str, ip: str) -> None:
        """登录前调用：计入一次尝试，超过上限抛 TooManyLoginAttempts（带剩余秒数）。"""
        keys = self._keys(name, ip)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                for key in (keys.pair, keys.ip):
                    pipe.incr(key)
                    pipe.expire(key, self.window, nx=True)
                    pipe.ttl(key)
                pair_count, _, pair_ttl, ip_count, _, ip_ttl = await pipe.execute()
        except (RedisError, OSError) as exc:
            log.error("login throttle unavailable, refusing the attempt", exc_info=True)
            raise LoginThrottleUnavailable() from exc
        waits = [ttl for count, limit, ttl in ((pair_count, self.pair_limit, pair_ttl), (ip_count, self.ip_limit, ip_ttl))
                 if count > limit]
        if waits:
            raise TooManyLoginAttempts(max(max(waits), 1))

    async def succeeded(self, name: str, ip: str) -> None:
        """登录成功：这次不算失败。清掉“账号 + IP”的计数，IP 计数退回这一次。"""
        keys = self._keys(name, ip)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.delete(keys.pair)
                pipe.decr(keys.ip)
                pipe.expire(keys.ip, self.window, nx=True)  # 计数恰好在这之间过期时，DECR 会建一个没有过期时间的新键
                await pipe.execute()
        except (RedisError, OSError):
            log.warning("login throttle unavailable, counters not reset", exc_info=True)  # 已经登录成功，不影响这次
