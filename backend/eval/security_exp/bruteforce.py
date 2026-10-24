"""实验 1：现在能多快地猜密码（decisions/0025 草稿）。

对正在运行的 api（不经 Nginx）并发发送错误密码的登录请求，统计每秒能尝试多少次、有没有被拦。
只打本机，账号用不存在的用户名和一个真实用户名各测一次（都用错误密码，不会登录成功）。
用法：python bruteforce.py [--base http://localhost:8000] [--user admin] [--n 400] [--concurrency 20]
"""

import argparse
import asyncio
import collections
import time

import httpx


async def attack(base: str, user: str, n: int, concurrency: int) -> None:
    codes: collections.Counter = collections.Counter()
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        async def one(i):
            async with sem:
                r = await c.post("/api/auth/login", json={"name": user, "password": f"wrong-{i}"})
                codes[r.status_code] += 1

        t = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        dt = time.perf_counter() - t
    print(f"  用户 {user!r}：{n} 次 / {dt:.1f}s = {n / dt:.0f} 次/秒，状态码 {dict(codes)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--user", default="admin")
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--concurrency", type=int, default=20)
    a = p.parse_args()
    print("并发错误密码登录：")
    asyncio.run(attack(a.base, a.user, a.n, a.concurrency))
    asyncio.run(attack(a.base, "no-such-user-xyz", a.n, a.concurrency))


if __name__ == "__main__":
    main()
