"""从 PostgreSQL 重建 Neo4j 里的图（decisions/0019）。

Neo4j 是派生存储：数据卷丢了、怀疑和数据库不一致、或者 Neo4j 换了版本，都可以用这个命令按知识库整体重写。
只读数据库里已经合并好的实体和关系，不调用模型、不花钱。
用法（在 api 容器里）：docker compose exec api python -m agentnexus.knowledge.graph.sync
"""

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.graph.store import Neo4jGraphStore
from agentnexus.storage.postgres import GraphEntity, GraphRelation, KnowledgeBase

log = logging.getLogger(__name__)


async def sync_all(sessionmaker: async_sessionmaker, store: Neo4jGraphStore) -> dict[uuid.UUID, tuple[int, int]]:
    """逐个知识库重写，返回 {知识库: (实体数, 关系数)}。没有图的知识库也会清掉 Neo4j 里的残留。"""
    await store.ensure_schema()
    async with sessionmaker() as session:
        kb_ids = list((await session.scalars(select(KnowledgeBase.id))).all())
    out = {}
    for kb_id in kb_ids:
        async with sessionmaker() as session:
            entities = list((await session.scalars(select(GraphEntity).where(GraphEntity.knowledge_base_id == kb_id))).all())
            relations = list((await session.scalars(select(GraphRelation).where(GraphRelation.knowledge_base_id == kb_id))).all())
        await store.replace_kb(kb_id, entities, relations)
        out[kb_id] = (len(entities), len(relations))
        if entities:
            log.info("synced knowledge base %s: %d entities, %d relations", kb_id, len(entities), len(relations))
    return out


async def _main() -> None:
    from neo4j import AsyncGraphDatabase

    from agentnexus.config import get_settings
    from agentnexus.observability.logging import configure_logging
    from agentnexus.storage.postgres import make_engine, make_sessionmaker

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.database_url)
    driver = AsyncGraphDatabase.driver(settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password or ""))
    try:
        result = await sync_all(make_sessionmaker(engine), Neo4jGraphStore(driver))
        print(f"synced {len(result)} knowledge base(s): {sum(e for e, _ in result.values())} entities, "
              f"{sum(r for _, r in result.values())} relations")
    finally:
        await driver.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
