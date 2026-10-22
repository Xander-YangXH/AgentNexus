"""多 Agent 编排与审批中断的实验公共部分（decisions/0022 草稿）。

用真实的 AsyncPostgresSaver（与生产相同的 checkpoint 存储），模型调用用 sleep 模拟（零费用）。
“台账”是一个 JSONL 文件：每个进程在每次“调用模型 / 执行子任务”时写一行，用来数重复执行了什么。
"""

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

ADMIN_URL = os.environ["BENCH_PG_URL"]
URL = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname="bench_agents")
LEDGER = Path(os.environ.get("LEDGER", "/tmp/anx_agents_ledger.jsonl"))


def reset():
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bench_agents")
        c.execute("CREATE DATABASE bench_agents")
    LEDGER.unlink(missing_ok=True)


@asynccontextmanager
async def saver():
    async with AsyncPostgresSaver.from_conn_string(URL) as cp:
        await cp.setup()
        yield cp


def ledger(event: str, **kw):
    with LEDGER.open("a") as f:
        f.write(json.dumps({"pid": os.getpid(), "event": event, "at": time.time(), **kw}, ensure_ascii=False) + "\n")


def read_ledger() -> list[dict]:
    return [json.loads(line) for line in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


def checkpoint_stats(thread_id: str) -> dict:
    with psycopg.connect(URL) as c:
        rows = c.execute("SELECT count(*), coalesce(sum(length(checkpoint::text)), 0) FROM checkpoints WHERE thread_id = %s",
                         (thread_id,)).fetchone()
        writes = c.execute("SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (thread_id,)).fetchone()[0]
    return {"checkpoints": rows[0], "checkpoint_bytes": rows[1], "pending_writes": writes}
