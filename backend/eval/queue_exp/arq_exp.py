"""ARQ 0.28：worker 被 kill -9 / 被暂停（SIGSTOP）时，任务多久被接手、会不会重复执行。

用法：BENCH_PG_URL=... python arq_exp.py
worker 用 arq 自带的命令行启动：arq arq_exp.WorkerSettings
"""

import asyncio
import os
import signal
import subprocess
import sys
import time

import psycopg
from arq import create_pool
from arq.connections import RedisSettings

from common import REDIS_URL, STEP_SECONDS, STEPS, URL, ledger, report, reset_db

JOB_TIMEOUT = 30  # ARQ 的“进行中”标记 = job_timeout + 10 秒，期间不续期


async def long_job(ctx, job):
    worker = os.environ.get("WORKER_NAME", str(os.getpid()))
    with psycopg.connect(URL, autocommit=True) as conn:
        ledger(conn, job, worker, None, "start")
        for step in range(1, STEPS + 1):
            await asyncio.sleep(STEP_SECONDS)
            ledger(conn, job, worker, step, "step")


class WorkerSettings:
    functions = [long_job]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    job_timeout = JOB_TIMEOUT
    max_tries = 5
    poll_delay = 0.5


def start_worker(name):
    env = {**os.environ, "WORKER_NAME": name, "PYTHONPATH": os.path.dirname(__file__)}
    return subprocess.Popen([sys.executable, "-m", "arq", "arq_exp.WorkerSettings"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ledger(job, predicate, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        with psycopg.connect(URL) as c:
            rows = c.execute("SELECT worker, step, event FROM ledger WHERE job = %s", (job,)).fetchall()
        if predicate(rows):
            return True
        time.sleep(0.2)
    return False


async def scenario(name, job, action):
    pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    await pool.flushdb()
    a = start_worker("A")
    t0 = time.time()
    await pool.enqueue_job("long_job", job, _job_id=job)
    wait_ledger(job, lambda rows: sum(r[2] == "step" for r in rows) >= 3)  # A 做完 3 步
    b = None
    if action == "kill":
        a.send_signal(signal.SIGKILL)
        t_event = time.time()
        b = start_worker("B")
    else:  # 暂停：模拟长时间 GC 停顿 / 网络卡住，进程其实还活着
        a.send_signal(signal.SIGSTOP)
        t_event = time.time()
        b = start_worker("B")
        wait_ledger(job, lambda rows: any(r[0] == "B" and r[2] == "start" for r in rows), timeout=90)
        a.send_signal(signal.SIGCONT)  # B 接手之后，A 恢复
    wait_ledger(job, lambda rows: sum(r[2] == "step" and r[0] == "B" for r in rows) >= STEPS, timeout=150)
    await asyncio.sleep(4)
    print(f"\n== ARQ：{name}（{t_event - t0:.1f}s 时发生）")
    dup = report(job, t0)
    with psycopg.connect(URL) as c:
        b_start = c.execute("SELECT min(at) FROM ledger WHERE job = %s AND worker = 'B' AND event = 'start'", (job,)).fetchone()[0]
    took = f"{b_start - t_event:.1f}s" if b_start else "没有接手"
    print(f"   → B 接手用时：{took}；重复执行的步骤：{dup} 次")
    for p in (a, b):
        if p and p.poll() is None:
            p.send_signal(signal.SIGCONT)
            p.kill()
    await pool.aclose()


async def main():
    reset_db()
    await scenario("worker 被 kill -9", "job-kill", "kill")
    await scenario("worker 被暂停后恢复", "job-pause", "pause")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    asyncio.run(main())
