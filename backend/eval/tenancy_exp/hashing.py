"""实验 1：密码哈希的耗时与并发影响（decisions/0023 草稿）。

登录要“故意慢”（拖慢离线暴力破解），但又不能把 api 卡住。测：
- argon2id（OWASP 推荐的参数 m=19MiB, t=2, p=1，以及 argon2-cffi 的默认参数）与 bcrypt（cost 10/12）的单次耗时；
- 在 asyncio 里直接调用 vs 放进线程池：同时有 8 个登录时，一个普通请求要等多久。
用法：python hashing.py
"""

import asyncio
import statistics
import time

import bcrypt
from argon2 import PasswordHasher

PASSWORD = "correct horse battery staple"
CANDIDATES = {
    "argon2id OWASP (m=19MiB,t=2,p=1)": PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1),
    "argon2id 库默认 (m=64MiB,t=3,p=4)": PasswordHasher(),
    "bcrypt cost=10": 10,
    "bcrypt cost=12": 12,
}


def hash_and_verify(c):
    if isinstance(c, PasswordHasher):
        h = c.hash(PASSWORD)
        c.verify(h, PASSWORD)
    else:
        h = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(c))
        bcrypt.checkpw(PASSWORD.encode(), h)


def verify_only(c):
    """登录时只做校验：先算一个哈希，再计时校验。"""
    if isinstance(c, PasswordHasher):
        h = c.hash(PASSWORD)
        t = time.perf_counter(); c.verify(h, PASSWORD); return time.perf_counter() - t
    h = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(c))
    t = time.perf_counter(); bcrypt.checkpw(PASSWORD.encode(), h); return time.perf_counter() - t


async def blocking_impact(c, offload: bool) -> float:
    """8 个并发登录 + 一个普通请求（sleep 0）：普通请求从发出到完成的时间。"""
    async def login():
        if offload:
            await asyncio.to_thread(verify_only, c)
        else:
            verify_only(c)

    # 计时起点是“请求到达”（创建任务的时刻），不是任务开始运行的时刻。
    # 第一版在 ping 内部开始计时，测出来两种方式都是 0ms——阻塞发生在 ping 开始运行之前，根本没被测到
    arrived = time.perf_counter()

    async def ping():
        await asyncio.sleep(0)
        return time.perf_counter() - arrived

    tasks = [asyncio.create_task(login()) for _ in range(8)]
    p = asyncio.create_task(ping())
    await asyncio.gather(*tasks)
    return await p


def main():
    print("单次校验耗时（中位数，5 次）：")
    for name, c in CANDIDATES.items():
        times = [verify_only(c) for _ in range(5)]
        print(f"  {name}: {statistics.median(times) * 1000:.0f} ms")
    c = CANDIDATES["argon2id OWASP (m=19MiB,t=2,p=1)"]
    for offload in (False, True):
        t = asyncio.run(blocking_impact(c, offload))
        print(f"8 个并发登录时普通请求的等待（{'线程池' if offload else '直接在事件循环里算'}）：{t * 1000:.0f} ms")


if __name__ == "__main__":
    main()
