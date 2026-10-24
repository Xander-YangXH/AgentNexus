"""实验 2：响应时间会不会泄露“用户名是否存在”（decisions/0025 草稿）。

用户不存在时，代码也会拿一个假哈希做一次同样参数的校验，理论上耗时一样。这里实测：
存在的用户、不存在的用户各顺序请求 N 次（错误密码），比较延迟的中位数和分布。交替进行，减少系统负载变化的影响。
用法：python timing.py [--base http://localhost:8000] [--user admin] [--n 200]
"""

import argparse
import statistics
import time

import httpx


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--user", default="admin")
    p.add_argument("--n", type=int, default=200)
    a = p.parse_args()
    times = {a.user: [], "no-such-user-xyz": []}
    with httpx.Client(base_url=a.base, timeout=30) as c:
        for i in range(a.n):
            for user in times:
                t = time.perf_counter()
                c.post("/api/auth/login", json={"name": user, "password": f"wrong-{i}"})
                times[user].append((time.perf_counter() - t) * 1000)
    for user, ts in times.items():
        ts.sort()
        print(f"  {user!r}: 中位数 {statistics.median(ts):.1f}ms，p10 {ts[len(ts) // 10]:.1f}ms，p90 {ts[len(ts) * 9 // 10]:.1f}ms")


if __name__ == "__main__":
    main()
