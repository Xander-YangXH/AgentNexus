"""实验：删除一个大知识库要多久（decisions/0026 草稿）。零费用，不留数据。

1. PostgreSQL：在一个事务里造一个知识库（N 篇文档 × M 个分块 + 图谱提及），计时“删文档（级联分块、提及）+ 删知识库”，
   最后整个事务回滚，不留任何数据；同时测删除期间另一个连接往别的知识库插入会不会被挡住。
2. Qdrant：建一个临时集合，写入 P 个点，比较“按 payload 过滤删除”和“按 id 删除”的耗时，最后删掉集合。
用法（本机，服务已启动）：DATABASE_URL=postgresql://<所有者>@localhost:5432/<库> python scale.py [--docs 1000 --chunks 100 --points 20000]
"""

import argparse
import asyncio
import os
import time
import uuid

import numpy as np
import psycopg
from qdrant_client import AsyncQdrantClient, models


def pg(url: str, docs: int, chunks: int) -> None:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        uid, kb = uuid.uuid4(), uuid.uuid4()
        cur.execute("insert into users (id, name) values (%s, %s)", (uid, f"exp-{uid.hex[:8]}"))
        cur.execute("insert into knowledge_bases (id, user_id, name) values (%s, %s, 'exp')", (kb, uid))
        t = time.perf_counter()
        cur.execute("""insert into documents (id, knowledge_base_id, filename, size_bytes, sha256, blob_key, status, chunk_count)
                       select gen_random_uuid(), %s, 'f' || i, 1, md5(i::text) || md5(i::text), 'documents/x', 'ready', %s
                       from generate_series(1, %s) i""", (kb, chunks, docs))
        cur.execute("""insert into document_chunks (id, document_id, knowledge_base_id, seq, text, token_count)
                       select gen_random_uuid(), d.id, %s, s, repeat('退货政策', 60), 120
                       from documents d, generate_series(1, %s) s where d.knowledge_base_id = %s""", (kb, chunks, kb))
        cur.execute("insert into kg_entities (id, knowledge_base_id, key, name, type, description, mention_count) "
                    "values (gen_random_uuid(), %s, 'e', 'e', 'concept', 'd', 1) returning id", (kb,))
        ent = cur.fetchone()[0]
        cur.execute("""insert into kg_mentions (knowledge_base_id, chunk_id, entity_id)
                       select %s, id, %s from document_chunks where knowledge_base_id = %s""", (kb, ent, kb))
        print(f"PostgreSQL：造数据 {docs} 篇 × {chunks} 块 = {docs * chunks} 个分块（+ 同样多的图谱提及），{time.perf_counter() - t:.1f}s")
        t = time.perf_counter()
        cur.execute("delete from documents where knowledge_base_id = %s", (kb,))
        n = cur.rowcount
        cur.execute("delete from knowledge_bases where id = %s", (kb,))
        print(f"  删除 {n} 篇文档（级联分块、提及）+ 知识库：{time.perf_counter() - t:.2f}s（事务内，随后回滚）")
        conn.rollback()


async def qdrant(url: str, points: int) -> None:
    client = AsyncQdrantClient(url=url)
    name = f"exp_delete_{uuid.uuid4().hex[:8]}"
    await client.create_collection(name, vectors_config=models.VectorParams(size=512, distance=models.Distance.COSINE))
    await client.create_payload_index(name, "document_id", models.PayloadSchemaType.KEYWORD)
    await client.create_payload_index(name, "knowledge_base_id", models.PayloadSchemaType.KEYWORD)
    try:
        kb, docs = str(uuid.uuid4()), [str(uuid.uuid4()) for _ in range(points // 100)]
        rng = np.random.default_rng(0)
        ids = [str(uuid.uuid4()) for _ in range(points)]
        for i in range(0, points, 2000):
            vec = rng.random((min(2000, points - i), 512), dtype=np.float32)
            await client.upsert(name, models.Batch(ids=ids[i:i + 2000], vectors=vec.tolist(), payloads=[
                {"knowledge_base_id": kb, "document_id": docs[(i + j) // 100]} for j in range(len(vec))]), wait=True)
        print(f"Qdrant：{points} 个点（{len(docs)} 篇文档）")
        t = time.perf_counter()
        await client.delete(name, models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=docs[0]))])), wait=True)
        print(f"  按文档过滤删除 100 个点：{(time.perf_counter() - t) * 1000:.0f}ms")
        t = time.perf_counter()
        await client.delete(name, models.PointIdsList(points=ids[100:200]), wait=True)
        print(f"  按 id 删除 100 个点：{(time.perf_counter() - t) * 1000:.0f}ms")
        t = time.perf_counter()
        await client.delete(name, models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="knowledge_base_id", match=models.MatchValue(value=kb))])), wait=True)
        left = (await client.count(name, exact=True)).count
        print(f"  按知识库过滤删除其余 {points - 200} 个点：{time.perf_counter() - t:.2f}s，剩余 {left}")
    finally:
        await client.delete_collection(name)
        await client.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docs", type=int, default=1000)
    p.add_argument("--chunks", type=int, default=100)
    p.add_argument("--points", type=int, default=20000)
    a = p.parse_args()
    pg(os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://"), a.docs, a.chunks)
    asyncio.run(qdrant(os.environ.get("QDRANT_URL", "http://localhost:6333"), a.points))


if __name__ == "__main__":
    main()
