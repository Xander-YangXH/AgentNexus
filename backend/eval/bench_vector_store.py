"""pgvector vs Qdrant：召回、延迟、带过滤检索（结果见 decisions/0014）。

用法（需要 docker）：
  docker run -d --rm --name bench-pgvector -e POSTGRES_PASSWORD=bench -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg16
  docker run -d --rm --name bench-qdrant -p 127.0.0.1:56333:6333 qdrant/qdrant:v1.19.2
  pip install numpy psycopg[binary] qdrant-client && python -u bench_vector_store.py [pgvector] [qdrant]
数据是随机向量：对近似索引偏难（召回绝对值偏低），但两边数据相同，比较是公平的。
"""

import time

import numpy as np
import psycopg
from qdrant_client import QdrantClient, models

N, DIM, TENANTS, QUERIES, K = 50_000, 512, 100, 100, 10
rng = np.random.default_rng(42)
X = rng.standard_normal((N, DIM)).astype(np.float32)
X /= np.linalg.norm(X, axis=1, keepdims=True)
TENANT = np.arange(N) % TENANTS
Q = rng.standard_normal((QUERIES, DIM)).astype(np.float32)
Q /= np.linalg.norm(Q, axis=1, keepdims=True)
Q_TENANT = rng.integers(0, TENANTS, QUERIES)

# 精确答案（暴力计算）：归一化向量的内积 = 余弦相似度
TRUTH = [set(np.argsort(-(X @ q))[:K]) for q in Q]
TRUTH_F = []
for q, t in zip(Q, Q_TENANT):
    idx = np.where(TENANT == t)[0]
    TRUTH_F.append(set(idx[np.argsort(-(X[idx] @ q))[:K]]))


def report(label, results, truths, times):
    recall = np.mean([len(set(r) & t) / K for r, t in zip(results, truths)])
    returned = np.mean([len(r) for r in results])
    print(f"  {label:44} 召回@10 {recall:5.3f} | 平均返回 {returned:5.1f} 条 | 延迟 p50 {np.percentile(times, 50):6.1f}ms p95 {np.percentile(times, 95):6.1f}ms")


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def bench_pgvector():
    print("== pgvector 0.8.7（PostgreSQL 16）")
    with psycopg.connect("postgresql://postgres:bench@127.0.0.1:55432/postgres", autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("DROP TABLE IF EXISTS items")
        conn.execute(f"CREATE TABLE items (id int PRIMARY KEY, tenant int NOT NULL, emb vector({DIM}) NOT NULL)")
        t0 = time.perf_counter()
        with conn.cursor().copy("COPY items (id, tenant, emb) FROM STDIN") as cp:
            for i in range(N):
                cp.write_row((i, int(TENANT[i]), vec_literal(X[i])))
        print(f"  写入 {N} 条：{time.perf_counter() - t0:.1f}s")
        t0 = time.perf_counter()
        conn.execute("SET maintenance_work_mem = '512MB'")
        conn.execute("CREATE INDEX ON items USING hnsw (emb vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
        print(f"  建 HNSW 索引：{time.perf_counter() - t0:.1f}s")
        conn.execute("ANALYZE items")
        size = conn.execute("SELECT pg_size_pretty(pg_total_relation_size('items'))").fetchone()[0]
        print(f"  表 + 索引占用：{size}")

        def run(sql_filter: bool, label: str, settings: list[str]):
            for s in settings:
                conn.execute(s)
            results, times = [], []
            for q, t in zip(Q, Q_TENANT):
                lit = vec_literal(q)
                where = "WHERE tenant = %s " if sql_filter else ""
                params = (int(t), lit) if sql_filter else (lit,)
                sql = f"SELECT id FROM items {where}ORDER BY emb <=> %s::vector LIMIT {K}"
                t0 = time.perf_counter()
                rows = conn.execute(sql, params).fetchall()
                times.append((time.perf_counter() - t0) * 1000)
                results.append([r[0] for r in rows])
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql, params).fetchall())
            node = "HNSW 索引" if "items_emb_idx" in plan else ("租户 B 树索引 + 排序" if "tenant_idx" in plan else "全表扫描 + 排序")
            report(f"{label}（计划：{node}）", results, TRUTH_F if sql_filter else TRUTH, times)

        run(False, "不过滤", ["SET hnsw.ef_search = 40", "SET hnsw.iterative_scan = off"])
        run(True, "过滤 1%：只有向量索引", ["SET hnsw.iterative_scan = off"])
        run(True, "过滤 1%：开启迭代扫描（relaxed_order）", ["SET hnsw.iterative_scan = relaxed_order"])
        conn.execute("CREATE INDEX items_tenant_idx ON items (tenant)")
        conn.execute("ANALYZE items")
        run(True, "过滤 1%：再加租户 B 树索引", ["SET hnsw.iterative_scan = off"])


def bench_qdrant():
    print("== Qdrant 1.19.2")
    client = QdrantClient(url="http://127.0.0.1:56333", timeout=60)
    name = "items"
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        name,
        vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE),
        hnsw_config=models.HnswConfigDiff(m=16, ef_construct=64),
    )
    # 先建租户字段的 payload 索引：Qdrant 会据此在 HNSW 图里为过滤条件补充连接（filterable HNSW）
    client.create_payload_index(name, "tenant", models.PayloadSchemaType.INTEGER)
    t0 = time.perf_counter()
    for start in range(0, N, 1000):
        end = min(start + 1000, N)
        client.upsert(
            name,
            points=models.Batch(ids=list(range(start, end)), vectors=X[start:end].tolist(),
                                payloads=[{"tenant": int(t)} for t in TENANT[start:end]]),
            wait=True,
        )
    print(f"  写入 {N} 条：{time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    # HNSW 在后台异步构建，等状态变 green（后台优化完成）再测。
    # 注意：不能要求“已索引数量接近总数”——小于阈值的数据段 Qdrant 故意不建 HNSW、直接精确扫描，
    # 已索引数量可能永远到不了总数（第一版脚本就因此死等，见 dev-log）
    while True:
        info = client.get_collection(name)
        if info.status == models.CollectionStatus.GREEN:
            break
        time.sleep(1)
    print(f"  等待索引建完：{time.perf_counter() - t0:.1f}s（已索引 {info.indexed_vectors_count} 条）")

    def run(filtered: bool, label: str):
        results, times = [], []
        for q, t in zip(Q, Q_TENANT):
            flt = models.Filter(must=[models.FieldCondition(key="tenant", match=models.MatchValue(value=int(t)))]) if filtered else None
            t0 = time.perf_counter()
            pts = client.query_points(name, query=q.tolist(), limit=K, query_filter=flt,
                                      search_params=models.SearchParams(hnsw_ef=40)).points
            times.append((time.perf_counter() - t0) * 1000)
            results.append([p.id for p in pts])
        report(label, results, TRUTH_F if filtered else TRUTH, times)

    run(False, "不过滤")
    run(True, "过滤 1%：租户 payload 索引")


def extra_pgvector():
    """复用 bench_pgvector 建好的表（不重建）：
    1) 不过滤时召回随 ef_search 的变化；2) 强制走 HNSW 时的过滤问题与迭代扫描。"""
    print("== pgvector 补充实验（复用已建好的表）")
    with psycopg.connect("postgresql://postgres:bench@127.0.0.1:55432/postgres", autocommit=True) as conn:
        def query(filtered):
            results, times = [], []
            for q, t in zip(Q, Q_TENANT):
                lit = vec_literal(q)
                where = "WHERE tenant = %s " if filtered else ""
                params = (int(t), lit) if filtered else (lit,)
                t0 = time.perf_counter()
                rows = conn.execute(f"SELECT id FROM items {where}ORDER BY emb <=> %s::vector LIMIT {K}", params).fetchall()
                times.append((time.perf_counter() - t0) * 1000)
                results.append([r[0] for r in rows])
            return results, times

        conn.execute("SET hnsw.iterative_scan = off")
        for ef in (40, 100, 200, 400):
            conn.execute(f"SET hnsw.ef_search = {ef}")
            report(f"不过滤 ef_search={ef}", *query(False)[:1], TRUTH, query(False)[1])
        conn.execute("SET hnsw.ef_search = 40")
        # 强制走 HNSW：去掉租户索引、禁止全表扫描，复现“先近似检索、再过滤”的问题
        conn.execute("DROP INDEX IF EXISTS items_tenant_idx")
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET hnsw.iterative_scan = off")
        report("过滤 1%：强制 HNSW，迭代扫描关闭", *query(True)[:1], TRUTH_F, query(True)[1])
        conn.execute("SET hnsw.iterative_scan = relaxed_order")
        report("过滤 1%：强制 HNSW，迭代扫描 relaxed_order", *query(True)[:1], TRUTH_F, query(True)[1])
        conn.execute("SET hnsw.iterative_scan = strict_order")
        report("过滤 1%：强制 HNSW，迭代扫描 strict_order", *query(True)[:1], TRUTH_F, query(True)[1])


def sweep_qdrant():
    print("== Qdrant 补充实验：不过滤时召回随 hnsw_ef 的变化（复用已建好的集合）")
    client = QdrantClient(url="http://127.0.0.1:56333", timeout=60)
    for ef in (40, 100, 200, 400):
        results, times = [], []
        for q in Q:
            t0 = time.perf_counter()
            pts = client.query_points("items", query=q.tolist(), limit=K, search_params=models.SearchParams(hnsw_ef=ef)).points
            times.append((time.perf_counter() - t0) * 1000)
            results.append([p.id for p in pts])
        report(f"不过滤 hnsw_ef={ef}", results, TRUTH, times)


if __name__ == "__main__":
    import sys

    which = sys.argv[1:] or ["pgvector", "qdrant"]
    if "pgvector" in which:
        bench_pgvector()
    if "qdrant" in which:
        bench_qdrant()
    if "pgvector-extra" in which:
        extra_pgvector()
    if "qdrant-sweep" in which:
        sweep_qdrant()
