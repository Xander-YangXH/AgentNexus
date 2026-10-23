"""实验 3：用量明细能不能直接聚合出 Dashboard（decisions/0023 草稿）。

每次模型调用 / 检索记一行明细（usage_events）。问题是：Dashboard 的查询直接在明细上 GROUP BY 够不够快，
还是需要预先汇总（按小时 / 用户的 rollup 表）？
数据：100 万行、30 天、2000 个用户、20 个部门（约是“每天 3 万次模型调用”的规模，远超演示环境）。
用法：BENCH_PG_URL=... python usage.py
"""

import os
import random
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

ADMIN = os.environ["BENCH_PG_URL"]
DB = "bench_usage"
URL = psycopg.conninfo.make_conninfo(ADMIN, dbname=DB)
ROWS, USERS, DEPTS, DAYS = 1_000_000, 2000, 20, 30

SCHEMA = """
CREATE TABLE usage_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL,
  user_id uuid NOT NULL,
  department_id uuid,
  kind text NOT NULL,            -- llm / search
  model text,
  input_tokens int NOT NULL DEFAULT 0,
  cached_tokens int NOT NULL DEFAULT 0,
  output_tokens int NOT NULL DEFAULT 0
);
CREATE INDEX ix_usage_user_time ON usage_events (user_id, created_at);
CREATE INDEX ix_usage_dept_time ON usage_events (department_id, created_at);
CREATE INDEX ix_usage_time ON usage_events (created_at);
"""

QUERIES = {
    "某用户 30 天每天的 token": """
        SELECT date_trunc('day', created_at) d, sum(input_tokens), sum(output_tokens), count(*) FILTER (WHERE kind='search')
        FROM usage_events WHERE user_id = %(u)s AND created_at >= %(since)s GROUP BY 1 ORDER BY 1""",
    "某部门 30 天每天的 token": """
        SELECT date_trunc('day', created_at) d, sum(input_tokens), sum(output_tokens)
        FROM usage_events WHERE department_id = %(d)s AND created_at >= %(since)s GROUP BY 1 ORDER BY 1""",
    "全站 7 天每小时": """
        SELECT date_trunc('hour', created_at) h, count(*), sum(input_tokens + output_tokens)
        FROM usage_events WHERE created_at >= %(week)s GROUP BY 1 ORDER BY 1""",
    "全站 30 天每天": """
        SELECT date_trunc('day', created_at) d, count(*), sum(input_tokens + output_tokens)
        FROM usage_events WHERE created_at >= %(since)s GROUP BY 1 ORDER BY 1""",
}


def setup():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    rnd = random.Random(3)
    depts = [uuid.uuid4() for _ in range(DEPTS)]
    users = [(uuid.uuid4(), rnd.choice(depts)) for _ in range(USERS)]
    now = datetime.now(timezone.utc)
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(SCHEMA)
        t = time.perf_counter()
        with c.cursor().copy("COPY usage_events (created_at, user_id, department_id, kind, model, input_tokens, "
                             "cached_tokens, output_tokens) FROM STDIN") as cp:
            for _ in range(ROWS):
                u, d = rnd.choice(users)
                at = now - timedelta(seconds=rnd.random() * DAYS * 86400)
                if rnd.random() < 0.3:
                    cp.write_row((at, u, d, "search", None, 0, 0, 0))
                else:
                    cp.write_row((at, u, d, "llm", "deepseek-flash", rnd.randint(500, 9000), rnd.randint(0, 4000),
                                  rnd.randint(50, 1500)))
        print(f"写入 {ROWS} 行（COPY）：{time.perf_counter() - t:.1f}s")
        c.execute("VACUUM ANALYZE usage_events")
        size = c.execute("SELECT pg_size_pretty(pg_total_relation_size('usage_events'))").fetchone()[0]
        print(f"表 + 索引大小：{size}")
    return users, now


def main():
    users, now = setup()
    rnd = random.Random(5)
    with psycopg.connect(URL) as c:
        # 单行插入的耗时：每次模型调用结束写一行，是不是可以接受
        times = []
        for _ in range(200):
            u, d = rnd.choice(users)
            t = time.perf_counter()
            c.execute("INSERT INTO usage_events (created_at, user_id, department_id, kind, input_tokens, output_tokens) "
                      "VALUES (now(), %s, %s, 'llm', 100, 10)", (u, d))
            c.commit()
            times.append(time.perf_counter() - t)
        print(f"单行插入（含提交）：中位数 {statistics.median(times) * 1000:.2f} ms")
        print("聚合查询（中位数 / 最大，毫秒，各 20 次）：")
        for name, sql in QUERIES.items():
            ts = []
            for _ in range(20):
                u, d = rnd.choice(users)
                t = time.perf_counter()
                c.execute(sql, {"u": u, "d": d, "since": now - timedelta(days=30), "week": now - timedelta(days=7)}).fetchall()
                ts.append(time.perf_counter() - t)
            print(f"  {name}：{statistics.median(ts) * 1000:.1f} / {max(ts) * 1000:.1f}")


if __name__ == "__main__":
    main()
