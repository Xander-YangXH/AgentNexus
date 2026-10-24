"""从 PostgreSQL 重建 Qdrant 索引（decisions/0016）。

什么时候用：换了 embedding 模型（维度或语义变了）、Qdrant 数据丢了或怀疑和数据库不一致。
用法（在 api 容器里）：docker compose exec api python -m agentnexus.knowledge.reindex

做法：建一个新集合 → 把所有 ready 文档的分块重新向量化写进去 → 原子地把别名切到新集合 → 删旧集合。
切换前检索一直用旧集合，不中断。重建期间新完成的文档写进的是旧集合，所以切换后按“完成时间不早于开始时间”
再补一遍（补的时候别名已指向新集合，之后完成的文档也直接进新集合）。
剩余的空档：某个文档在切换前写入旧集合、却在补录查询之后才提交 ready——概率很小，需要时再跑一次即可。
向量不存数据库，重建要重新计算：本地模型不花钱，只花 CPU 时间。
"""

import asyncio
import datetime as dt
import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.embedding import Embedder
from agentnexus.storage.postgres import Document, DocumentChunk, DocumentStatus
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint

log = logging.getLogger(__name__)

BATCH = 256


async def rebuild(sessionmaker: async_sessionmaker, embedder: Embedder, index: ChunkIndex) -> int:
    """返回写入的点数。"""
    async with sessionmaker() as session:
        started = (await session.execute(text("SELECT now()"))).scalar_one()  # 用数据库的时钟，与 processed_at 一致
    new = await index.create_collection()
    try:
        n = await _fill(sessionmaker, embedder, index, new, since=None)
    except BaseException:
        await index.drop(new)  # 没切换之前失败：删掉半成品，旧索引不受影响
        raise
    old = await index.switch_alias(new)
    n += await _fill(sessionmaker, embedder, index, new, since=started)
    if old is not None:
        await index.drop(old)
    return n


async def _fill(sessionmaker, embedder: Embedder, index: ChunkIndex, collection: str, *, since: dt.datetime | None) -> int:
    total = 0
    after: uuid.UUID | None = None
    while True:
        # 按分块 id 翻页（键集分页）：不用 OFFSET，翻到后面也不变慢
        stmt = (
            select(DocumentChunk.id, DocumentChunk.knowledge_base_id, DocumentChunk.document_id, DocumentChunk.text)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.status == DocumentStatus.READY)
            .order_by(DocumentChunk.id)
            .limit(BATCH)
        )
        if since is not None:
            stmt = stmt.where(Document.processed_at >= since)
        if after is not None:
            stmt = stmt.where(DocumentChunk.id > after)
        async with sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        if not rows:
            return total
        vectors = await asyncio.to_thread(embedder.embed_documents, [r.text for r in rows])
        await index.upsert(
            [ChunkPoint(r.id, r.knowledge_base_id, r.document_id, v) for r, v in zip(rows, vectors, strict=True)],
            collection=collection,
        )
        total += len(rows)
        after = rows[-1].id
        log.info("reindexed %d chunk(s)", total)


async def _main() -> None:
    from qdrant_client import AsyncQdrantClient

    from agentnexus.config import get_settings
    from agentnexus.knowledge.embedding import FastEmbedder
    from agentnexus.observability.logging import configure_logging
    from agentnexus.storage.postgres import make_engine, make_sessionmaker

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.database_url)
    embedder = FastEmbedder(settings.embedding_model)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=60)
    try:
        n = await rebuild(make_sessionmaker(engine), embedder, ChunkIndex(qdrant, settings.qdrant_collection, embedder.dim))
        print(f"reindexed {n} chunk(s) into alias {settings.qdrant_collection!r}")
    finally:
        await qdrant.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
