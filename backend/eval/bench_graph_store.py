"""知识图谱存储选型基准（decisions/0019 草稿）：同一份合成图，PostgreSQL（邻接表）vs Neo4j。

准备：
  docker run -d --rm --name bench-neo4j -p 127.0.0.1:57687:7687 \\
    -e NEO4J_AUTH=neo4j/benchpassword123 -e NEO4J_server_memory_heap_max__size=1g \\
    -e NEO4J_server_memory_pagecache_size=512m -e NEO4J_dbms_usage__report_enabled=false neo4j:2026.08.1-community
  PostgreSQL 用项目 compose 里的实例：BENCH_PG_URL 指向任意可连接的库，脚本会新建 / 删除 bench_graph 库。
用法：pip install neo4j psycopg[binary]；BENCH_PG_URL=... python bench_graph_store.py
"""

import os
import random
import statistics
import time

import psycopg
from neo4j import GraphDatabase
from psycopg import sql

KBS = [(0, 50_000, 150_000)] + [(k, 15_000, 45_000) for k in range(1, 10)]  # (知识库, 实体数, 关系数)
TARGET_KB = 0
RUNS = 40
WORDS = ["退货", "发票", "会员", "订单", "物流", "积分", "客服", "支付", "账户", "优惠", "仓库", "售后", "保修", "配送", "企业"]


HUBS = 20  # 每个知识库里的枢纽实体数（如“订单”“会员”这类几乎处处出现的概念）
HUB_SHARE = 0.3  # 连到枢纽的关系占比


def make_graph(seed=7):
    """少数枢纽 + 大量低度节点。

    第一版用“偏好连接”（被选中过的端点再放回池子）想造出枢纽，结果最大度数只有 81、中位数 5——池子被大量随机端点稀释，
    其实没有枢纽，测不出多跳查询在枢纽上的代价。改为显式指定 20 个枢纽，30% 的关系连到它们，按 Zipf 分配（第 k 个权重 1/k）。
    """
    rng = random.Random(seed)
    entities, relations, next_id = [], [], 0
    weights = [1 / k for k in range(1, HUBS + 1)]
    for kb, n_ent, n_rel in KBS:
        ids = list(range(next_id, next_id + n_ent))
        next_id += n_ent
        entities += [(i, kb, f"{rng.choice(WORDS)}{rng.choice(WORDS)}{i}") for i in ids]
        hubs = ids[:HUBS]
        for _ in range(n_rel):
            a = rng.choices(hubs, weights)[0] if rng.random() < HUB_SHARE else rng.choice(ids)
            b = rng.choice(ids)
            if a != b:
                relations.append((kb, a, b))
    return entities, relations


def timed(fn, args_list):
    out = []
    for args in args_list:
        t = time.perf_counter()
        fn(*args)
        out.append((time.perf_counter() - t) * 1000)
    return f"p50 {statistics.median(out):7.1f}ms  p95 {sorted(out)[int(len(out) * 0.95) - 1]:8.1f}ms"


def bench_pg(entities, relations, samples):
    admin = os.environ["BENCH_PG_URL"]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bench_graph")
        c.execute("CREATE DATABASE bench_graph")
    url = psycopg.conninfo.make_conninfo(admin, dbname="bench_graph")
    with psycopg.connect(url, autocommit=True) as c:
        t = time.perf_counter()
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        c.execute("CREATE TABLE ent (id bigint PRIMARY KEY, kb int, name text)")
        c.execute("CREATE TABLE rel (kb int, src bigint, dst bigint)")
        with c.cursor().copy("COPY ent FROM STDIN") as cp:
            for row in entities:
                cp.write_row(row)
        with c.cursor().copy("COPY rel FROM STDIN") as cp:
            for row in relations:
                cp.write_row(row)
        c.execute("CREATE INDEX ON rel (src)")
        c.execute("CREATE INDEX ON rel (dst)")
        c.execute("CREATE INDEX ent_name_trgm ON ent USING gin (name gin_trgm_ops)")
        c.execute("ANALYZE")
        print(f"== PostgreSQL 16：导入 + 建索引 {time.perf_counter() - t:.1f}s，"
              f"占用 {c.execute('SELECT pg_size_pretty(pg_database_size(current_database()))').fetchone()[0]}")

        def search(word):
            c.execute("SELECT id, name FROM ent WHERE kb = %s AND name LIKE %s LIMIT 20", (TARGET_KB, f"%{word}%")).fetchall()

        def one_hop(x):
            c.execute("(SELECT dst FROM rel WHERE src = %s LIMIT 50) UNION (SELECT src FROM rel WHERE dst = %s LIMIT 50)", (x, x)).fetchall()

        def two_hop(x):
            c.execute("""
                WITH h1 AS (SELECT dst AS id FROM rel WHERE src = %(x)s UNION SELECT src FROM rel WHERE dst = %(x)s),
                     h2 AS (SELECT r.dst AS id FROM rel r JOIN h1 ON r.src = h1.id
                            UNION SELECT r.src FROM rel r JOIN h1 ON r.dst = h1.id)
                SELECT id FROM (SELECT id FROM h1 UNION SELECT id FROM h2) t LIMIT 200""", {"x": x}).fetchall()

        def neighbors(frontier):
            rows = c.execute("SELECT src, dst FROM rel WHERE src = ANY(%(f)s) UNION ALL SELECT dst, src FROM rel WHERE dst = ANY(%(f)s)",
                             {"f": list(frontier)}).fetchall()
            return rows

        def shortest_app(a, b, max_hops=4):
            """应用层双向广度优先：每一层一条查询，从两端交替扩展较小的一侧。"""
            if a == b:
                return [a]
            fa, fb, pa, pb = {a}, {b}, {a: None}, {b: None}
            for _ in range(max_hops):
                if len(fa) <= len(fb):
                    nxt = set()
                    for u, v in neighbors(fa):
                        if v not in pa:
                            pa[v] = u
                            nxt.add(v)
                    fa = nxt
                else:
                    nxt = set()
                    for u, v in neighbors(fb):
                        if v not in pb:
                            pb[v] = u
                            nxt.add(v)
                    fb = nxt
                meet = pa.keys() & pb.keys()
                if meet:
                    return meet
            return None

        def shortest_cte(a, b):
            try:
                c.execute("SET statement_timeout = 3000")
                c.execute("""
                    WITH RECURSIVE bfs(id, depth, path) AS (
                      SELECT %(a)s::bigint, 0, ARRAY[%(a)s::bigint]
                      UNION ALL
                      SELECT n.nb, b.depth + 1, b.path || n.nb FROM bfs b
                      JOIN LATERAL (SELECT dst AS nb FROM rel WHERE src = b.id UNION ALL SELECT src FROM rel WHERE dst = b.id) n ON true
                      WHERE b.depth < 4 AND NOT n.nb = ANY(b.path))
                    SELECT path FROM bfs WHERE id = %(b)s ORDER BY depth LIMIT 1""", {"a": a, "b": b}).fetchall()
            except psycopg.errors.QueryCanceled:
                pass
            finally:
                c.execute("SET statement_timeout = 0")

        run_all("PostgreSQL", samples, search, one_hop, two_hop, shortest_app, shortest_cte)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE bench_graph")


def bench_neo4j(entities, relations, samples):
    driver = GraphDatabase.driver("bolt://127.0.0.1:57687", auth=("neo4j", "benchpassword123"))
    with driver.session() as s:
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        t = time.perf_counter()
        s.run("CREATE CONSTRAINT ent_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE").consume()
        s.run("CREATE TEXT INDEX ent_name IF NOT EXISTS FOR (e:Entity) ON (e.name)").consume()
        s.run("CREATE INDEX ent_kb IF NOT EXISTS FOR (e:Entity) ON (e.kb)").consume()
        for i in range(0, len(entities), 20_000):
            s.run("UNWIND $rows AS r CREATE (:Entity {id: r[0], kb: r[1], name: r[2]})", rows=entities[i:i + 20_000]).consume()
        for i in range(0, len(relations), 20_000):
            s.run("UNWIND $rows AS r MATCH (a:Entity {id: r[1]}), (b:Entity {id: r[2]}) CREATE (a)-[:REL {kb: r[0]}]->(b)",
                  rows=relations[i:i + 20_000]).consume()
        s.run("CALL db.awaitIndexes()").consume()
        print(f"== Neo4j 2026.08.1：导入 + 建索引 {time.perf_counter() - t:.1f}s")

        def search(word):
            s.run("MATCH (e:Entity) WHERE e.kb = $kb AND e.name CONTAINS $w RETURN e.id, e.name LIMIT 20", kb=TARGET_KB, w=word).data()

        def one_hop(x):
            s.run("MATCH (:Entity {id: $x})--(n) RETURN DISTINCT n.id LIMIT 100", x=x).data()

        def two_hop(x):
            s.run("MATCH (:Entity {id: $x})-[*1..2]-(n) RETURN DISTINCT n.id LIMIT 200", x=x).data()

        def shortest(a, b):
            s.run("MATCH (a:Entity {id: $a}), (b:Entity {id: $b}) MATCH p = shortestPath((a)-[*..4]-(b)) RETURN length(p)", a=a, b=b).data()

        run_all("Neo4j", samples, search, one_hop, two_hop, shortest, None)
    driver.close()


def run_all(name, samples, search, one_hop, two_hop, shortest, shortest_cte):
    words, nodes, hubs, pairs = samples
    print(f"   [{name}] Q1 名称包含关键词        {timed(search, [(w,) for w in words])}")
    print(f"   [{name}] Q2 一跳邻居（普通节点）  {timed(one_hop, [(x,) for x in nodes])}")
    print(f"   [{name}] Q2 一跳邻居（枢纽节点）  {timed(one_hop, [(x,) for x in hubs])}")
    print(f"   [{name}] Q3 两跳子图（普通节点）  {timed(two_hop, [(x,) for x in nodes])}")
    print(f"   [{name}] Q3 两跳子图（枢纽节点）  {timed(two_hop, [(x,) for x in hubs])}")
    label = "应用层双向 BFS" if shortest_cte else "shortestPath"
    print(f"   [{name}] Q4 最短路径（{label}）  {timed(shortest, [p for p in pairs])}")
    if shortest_cte:
        print(f"   [{name}] Q4 最短路径（递归 CTE，3 秒超时）{timed(shortest_cte, pairs[:5])}")


def main():
    entities, relations = make_graph()
    rng = random.Random(1)
    deg = {}
    for _, a, b in relations:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    target = [e[0] for e in entities if e[1] == TARGET_KB]
    hubs = sorted(target, key=lambda i: -deg.get(i, 0))[:RUNS]
    print(f"合成图：{len(entities)} 个实体、{len(relations)} 条关系；被测知识库 {len(target)} 个实体，"
          f"最大度数 {deg[hubs[0]]}，度数中位数 {statistics.median(deg.get(i, 0) for i in target)}")
    samples = ([rng.choice(WORDS) for _ in range(RUNS)], rng.sample(target, RUNS), hubs,
               [(rng.choice(target), rng.choice(target)) for _ in range(RUNS)])
    bench_pg(entities, relations, samples)
    bench_neo4j(entities, relations, samples)


if __name__ == "__main__":
    main()
