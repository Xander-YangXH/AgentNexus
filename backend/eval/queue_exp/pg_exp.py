"""PostgreSQL 租约队列原型：SKIP LOCKED 认领 + 租约 + 心跳 + fencing token + 按进度续做 + LISTEN/NOTIFY 唤醒。

与 arq_exp.py 同样的两个场景（kill -9、暂停后恢复），外加“入队 → 开始执行”的延迟。
用法：BENCH_PG_URL=... python pg_exp.py
"""

import asyncio
import os
import signal
import statistics
import subprocess
import sys
import time

import psycopg

from common import STEP_SECONDS, STEPS, URL, report, reset_db

LEASE = 6.0  # 租约时长（秒）
HEARTBEAT = 2.0  # 心跳间隔：租约的 1/3，丢一两次心跳也不会误判
POLL = 1.0  # 没有通知时多久查一次（用来发现租约过期的任务）

CLAIM = """
UPDATE jobs SET status = 'running', owner = %(w)s, version = version + 1, lease_until = clock_timestamp() + %(lease)s * interval '1 second'
WHERE id = (SELECT id FROM jobs
            WHERE status = 'queued' OR (status = 'running' AND lease_until < clock_timestamp())
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, version, progress, steps
"""


class LeaseLost(Exception):
    pass


async def run_worker(name):
    conn = await psycopg.AsyncConnection.connect(URL, autocommit=True)
    listen = await psycopg.AsyncConnection.connect(URL, autocommit=True)
    await listen.execute("LISTEN jobs")
    while True:
        row = await (await conn.execute(CLAIM, {"w": name, "lease": LEASE})).fetchone()
        if row is None:
            # 有新任务的通知就立刻再抢；否则最多等 POLL 秒（发现过期租约）。
            # 第一版用 asyncio.wait_for(anext(生成器), POLL)：超时取消 anext 会把异步生成器关掉，下一次读就报错退出——
            # 没收到过通知的 worker 第一次空等就崩了（实测踩到）。改用驱动自带的超时
            async for _ in listen.notifies(timeout=POLL, stop_after=1):
                pass
            continue
        job, version, progress, steps = row
        await conn.execute("INSERT INTO ledger VALUES (%s, %s, NULL, %s, extract(epoch from clock_timestamp()))",
                           (job, name, "start" if progress == 0 else f"resume@{progress}"))
        task = asyncio.create_task(execute(conn, name, job, version, progress, steps))
        hb = asyncio.create_task(heartbeat(name, job, version, task))
        try:
            await task
        except (LeaseLost, asyncio.CancelledError):
            await conn.execute("INSERT INTO ledger VALUES (%s, %s, NULL, 'fenced', extract(epoch from clock_timestamp()))", (job, name))
        finally:
            hb.cancel()


async def heartbeat(name, job, version, task):
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as c:
        while True:
            await asyncio.sleep(HEARTBEAT)
            cur = await c.execute("UPDATE jobs SET lease_until = clock_timestamp() + %s * interval '1 second' "
                                  "WHERE id = %s AND version = %s", (LEASE, job, version))
            if cur.rowcount == 0:  # 租约已被别人接管：停止执行
                task.cancel()
                return


async def execute(conn, name, job, version, progress, steps):
    for step in range(progress + 1, steps + 1):
        await asyncio.sleep(STEP_SECONDS)
        # 副作用与进度在同一个事务里，并用 fencing token（version）校验：被接管后，旧 worker 的写入一定失败
        async with conn.transaction():
            cur = await conn.execute("UPDATE jobs SET progress = %s WHERE id = %s AND version = %s", (step, job, version))
            if cur.rowcount == 0:
                raise LeaseLost
            await conn.execute("INSERT INTO ledger VALUES (%s, %s, %s, 'step', extract(epoch from clock_timestamp()))",
                               (job, name, step))
    await conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s AND version = %s", (job, version))


def start_worker(name):
    return subprocess.Popen([sys.executable, __file__, "worker", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def enqueue(job, steps=STEPS):
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("INSERT INTO jobs (id, steps) VALUES (%s, %s)", (job, steps))
        c.execute("NOTIFY jobs")


def wait(job, predicate, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        with psycopg.connect(URL) as c:
            rows = c.execute("SELECT worker, step, event FROM ledger WHERE job = %s", (job,)).fetchall()
        if predicate(rows):
            return
        time.sleep(0.1)


def scenario(name, job, action):
    a = start_worker("A")
    time.sleep(1.0)
    t0 = time.time()
    enqueue(job)
    wait(job, lambda rows: sum(r[2] == "step" for r in rows) >= 3)
    if action == "kill":
        a.send_signal(signal.SIGKILL)
    else:
        a.send_signal(signal.SIGSTOP)
    t_event = time.time()
    b = start_worker("B")
    if action == "pause":
        wait(job, lambda rows: any(r[0] == "B" for r in rows), timeout=60)
        a.send_signal(signal.SIGCONT)
    wait(job, lambda rows: sum(r[2] == "step" for r in rows) >= STEPS, timeout=90)
    time.sleep(4)
    print(f"\n== PostgreSQL 租约：{name}（{t_event - t0:.1f}s 时发生）")
    dup = report(job, t0)
    with psycopg.connect(URL) as c:
        b_start = c.execute("SELECT min(at) FROM ledger WHERE job = %s AND worker = 'B'", (job,)).fetchone()[0]
    print(f"   → B 接手用时：{b_start - t_event:.1f}s；重复执行的步骤：{dup} 次")
    for p in (a, b):
        if p.poll() is None:
            p.send_signal(signal.SIGCONT)
            p.kill()


def latency():
    w = start_worker("L")
    time.sleep(1.5)
    out = []
    for i in range(20):
        t = time.time()
        enqueue(f"lat-{i}", steps=0)
        wait(f"lat-{i}", lambda rows: bool(rows), timeout=10)
        with psycopg.connect(URL) as c:
            out.append((c.execute("SELECT min(at) FROM ledger WHERE job = %s", (f"lat-{i}",)).fetchone()[0] - t) * 1000)
        time.sleep(0.3)
    w.kill()
    print(f"\n== 入队 → 开始执行（LISTEN/NOTIFY）：中位数 {statistics.median(out):.1f}ms，最大 {max(out):.1f}ms（20 次）")


def main():
    reset_db()
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("""CREATE TABLE jobs (id text PRIMARY KEY, status text NOT NULL DEFAULT 'queued', owner text,
                     version int NOT NULL DEFAULT 0, lease_until timestamptz, progress int NOT NULL DEFAULT 0, steps int NOT NULL)""")
    scenario("worker 被 kill -9", "job-kill", "kill")
    scenario("worker 被暂停后恢复", "job-pause", "pause")
    latency()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "worker":
        asyncio.run(run_worker(sys.argv[2]))
    else:
        main()
