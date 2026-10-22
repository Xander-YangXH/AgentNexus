"""任务队列选型实验的公共部分（decisions/0020 草稿）：一个“台账”表记录每个 worker 对每个任务做了哪一步。

任务模型：一个任务分 STEPS 步，每步耗时 STEP_SECONDS，每步完成时写一条台账——相当于“调用一次模型 / 执行一次工具”这类有副作用、
有成本的操作。实验看的是：worker 崩溃或暂停后，同一步有没有被做两次，以及多久才有人接手。
"""

import os
import time

import psycopg

STEPS = 8
STEP_SECONDS = 2.0
ADMIN_URL = os.environ["BENCH_PG_URL"]
URL = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname="bench_queue")
REDIS_URL = os.environ.get("BENCH_REDIS_URL", "redis://localhost:6379/4")


def reset_db():
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bench_queue")
        c.execute("CREATE DATABASE bench_queue")
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("CREATE TABLE ledger (job text, worker text, step int, event text, at double precision)")


def ledger(conn, job, worker, step, event):
    conn.execute("INSERT INTO ledger VALUES (%s, %s, %s, %s, %s)", (job, worker, step, event, time.time()))


def report(job, t0):
    with psycopg.connect(URL) as c:
        rows = c.execute("SELECT worker, step, event, at FROM ledger WHERE job = %s ORDER BY at", (job,)).fetchall()
    done = [r for r in rows if r[2] == "step"]
    dup = len(done) - len({r[1] for r in done})
    for w, s, e, at in rows:
        print(f"    {at - t0:6.1f}s  {w:<10} {e:<9} {'' if s is None else s}")
    return dup
