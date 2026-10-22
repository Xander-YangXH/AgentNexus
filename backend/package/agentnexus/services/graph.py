"""知识图谱用例（阶段 3，decisions/0019）：预估费用 → 创建构建 → 后台执行（抽取、合并、同步 Neo4j）→ 浏览与溯源。

一致性：PostgreSQL 是事实来源（抽取缓存、实体、关系、出处、构建记录），Neo4j 是派生的图查询存储。
构建的最后一步把整个知识库的子图同步到 Neo4j；同步失败，构建记为失败，重试时抽取全部命中缓存，只重新同步。
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.graph.extraction import PROMPT_VERSION, GraphExtractor, estimate_tokens
from agentnexus.knowledge.graph.merge import merge
from agentnexus.knowledge.graph.store import Neo4jGraphStore
from agentnexus.runtime.failures import classify
from agentnexus.services.errors import (
    EntityNotFound,
    GraphBuildInProgress,
    GraphBuildTooLarge,
    KnowledgeBaseEmpty,
    KnowledgeBaseNotFound,
)
from agentnexus.storage.postgres import (
    Document,
    DocumentChunk,
    DocumentStatus,
    GraphBuild,
    GraphBuildStatus,
    GraphEntity,
    GraphExtraction,
    GraphMention,
    GraphRelation,
    KnowledgeBase,
)

log = logging.getLogger(__name__)

IN_BATCH = 1000  # IN (...) 列表的分批大小


@dataclass(frozen=True)
class Prices:
    """美元 / 百万 token。"""

    input: float = 0.3
    cached_input: float = 0.006
    output: float = 1.2

    def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        return ((input_tokens - cached_tokens) * self.input + cached_tokens * self.cached_input
                + output_tokens * self.output) / 1e6


@dataclass(frozen=True)
class _Chunk:
    id: uuid.UUID
    text: str
    title: str


class GraphService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        extractor: GraphExtractor,
        store: Neo4jGraphStore,
        *,
        max_chunks_per_build: int = 2000,
        concurrency: int = 4,
        prices: Prices = Prices(),
    ):
        self.sessionmaker = sessionmaker
        self.extractor = extractor
        self.store = store
        self.max_chunks = max_chunks_per_build
        self.concurrency = concurrency
        self.prices = prices

    # ---------- 预估与创建 ----------

    async def estimate(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> dict:
        await self._owned_kb(user_id, kb_id)
        chunks = await self._chunks(kb_id)
        groups = _group(chunks)
        cached = await self._cached(kb_id, list(groups))
        missing = [g for g in groups if g not in cached]
        est_in, est_out = estimate_tokens([len(groups[h][0].text) for h in missing])
        return {
            "chunk_count": len(chunks),
            "unique_count": len(groups),
            "cached_count": len(groups) - len(missing),
            "extract_count": len(missing),
            "estimated_cost_usd": round(self.prices.cost(est_in, 0, est_out), 4),  # 按未命中缓存的输入价，偏保守
            "max_extract_count": self.max_chunks,
        }

    async def create_build(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> GraphBuild:
        est = await self.estimate(user_id, kb_id)
        if est["chunk_count"] == 0:
            raise KnowledgeBaseEmpty(str(kb_id))
        if est["extract_count"] > self.max_chunks:
            raise GraphBuildTooLarge(est["extract_count"], self.max_chunks)
        try:
            async with self.sessionmaker() as session, session.begin():
                build = GraphBuild(
                    knowledge_base_id=kb_id, status=GraphBuildStatus.PENDING, chunk_count=est["chunk_count"],
                    unique_count=est["unique_count"], extract_count=est["extract_count"],
                    estimated_cost_usd=est["estimated_cost_usd"],
                )
                session.add(build)
                await session.flush()
                return build
        except IntegrityError as exc:
            if "uq_kg_builds_one_active_per_kb" in str(exc.orig):
                raise GraphBuildInProgress(str(kb_id)) from exc
            raise

    async def list_builds(self, user_id: uuid.UUID, kb_id: uuid.UUID, limit: int = 10) -> list[GraphBuild]:
        await self._owned_kb(user_id, kb_id)
        async with self.sessionmaker() as session:
            stmt = (select(GraphBuild).where(GraphBuild.knowledge_base_id == kb_id)
                    .order_by(GraphBuild.created_at.desc()).limit(limit))
            return list((await session.scalars(stmt)).all())

    # ---------- 后台执行（JobDispatcher 调用） ----------

    async def list_pending_ids(self) -> list[uuid.UUID]:
        async with self.sessionmaker() as session:
            stmt = select(GraphBuild.id).where(GraphBuild.status == GraphBuildStatus.PENDING).order_by(GraphBuild.created_at)
            return list((await session.scalars(stmt)).all())

    async def requeue_interrupted(self) -> list[uuid.UUID]:
        """启动恢复：执行到一半的构建改回 pending。可以安全重做：已抽取的结果在缓存里，重做不再花钱。"""
        async with self.sessionmaker() as session, session.begin():
            stmt = (update(GraphBuild).where(GraphBuild.status == GraphBuildStatus.RUNNING)
                    .values(status=GraphBuildStatus.PENDING).returning(GraphBuild.id))
            return list((await session.scalars(stmt)).all())

    async def process(self, build_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as session, session.begin():
            claimed = await session.execute(
                update(GraphBuild)
                .where(GraphBuild.id == build_id, GraphBuild.status == GraphBuildStatus.PENDING)
                .values(status=GraphBuildStatus.RUNNING, started_at=func.now())
            )
            if claimed.rowcount != 1:
                return False  # 已被别人抢走、或已经结束
            build = await session.get(GraphBuild, build_id)
        started = time.perf_counter()
        try:
            entity_count, relation_count = await self._run(build)
        except Exception as exc:
            code = _error_code(exc)
            log.warning("graph build %s failed: %s", build_id, code, exc_info=True)
            await self._finish(build_id, GraphBuildStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000], error_code=code)
            return True
        await self._finish(build_id, GraphBuildStatus.SUCCEEDED, entity_count=entity_count, relation_count=relation_count)
        log.info("graph build %s succeeded: %d entities, %d relations, %.1fs", build_id, entity_count, relation_count,
                 time.perf_counter() - started)
        return True

    async def _run(self, build: GraphBuild) -> tuple[int, int]:
        kb_id = build.knowledge_base_id
        groups = _group(await self._chunks(kb_id))
        cached = await self._cached(kb_id, list(groups))
        missing = [h for h in groups if h not in cached]
        if len(missing) > self.max_chunks:  # 创建之后又上传了很多文档：执行时再检查一次
            raise GraphBuildTooLarge(len(missing), self.max_chunks)

        # 只对没有缓存的文本调用模型；每抽完一个立刻写缓存、更新进度——中途失败或进程重启，已花的钱不白花
        sem = asyncio.Semaphore(self.concurrency)

        async def extract(text_hash: str):
            first = groups[text_hash][0]
            async with sem:
                result = await self.extractor.extract(first.title, first.text)
            async with self.sessionmaker() as session, session.begin():
                session.add(GraphExtraction(
                    knowledge_base_id=kb_id, text_sha256=text_hash, prompt_version=PROMPT_VERSION, payload=result.payload,
                    input_tokens=result.input_tokens, cached_tokens=result.cached_tokens, output_tokens=result.output_tokens,
                ))
                await session.execute(
                    update(GraphBuild).where(GraphBuild.id == build.id).values(
                        done_count=GraphBuild.done_count + 1,
                        input_tokens=GraphBuild.input_tokens + result.input_tokens,
                        cached_tokens=GraphBuild.cached_tokens + result.cached_tokens,
                        output_tokens=GraphBuild.output_tokens + result.output_tokens,
                    )
                )
            cached[text_hash] = result.payload

        # TaskGroup：任何一个失败就取消其余的，不再继续花钱
        async with asyncio.TaskGroup() as tg:
            for text_hash in missing:
                tg.create_task(extract(text_hash))

        entities, relations = merge([(c.id, cached[h]) for h, cs in groups.items() for c in cs])
        async with self.sessionmaker() as session, session.begin():
            # 整体重写这个知识库的图（删实体会级联删掉关系和出处）
            await session.execute(delete(GraphEntity).where(GraphEntity.knowledge_base_id == kb_id))
            if entities:
                await session.execute(insert(GraphEntity), [
                    {"id": e.id, "knowledge_base_id": kb_id, "key": e.key, "name": e.name, "type": e.type,
                     "description": e.description, "mention_count": len(e.chunk_ids)} for e in entities])
            if relations:
                await session.execute(insert(GraphRelation), [
                    {"id": r.id, "knowledge_base_id": kb_id, "source_id": r.source_id, "target_id": r.target_id,
                     "predicate": r.predicate, "description": r.description, "mention_count": len(r.chunk_ids)}
                    for r in relations])
            mentions = [{"knowledge_base_id": kb_id, "chunk_id": c, "entity_id": e.id, "relation_id": None}
                        for e in entities for c in e.chunk_ids]
            mentions += [{"knowledge_base_id": kb_id, "chunk_id": c, "entity_id": None, "relation_id": r.id}
                         for r in relations for c in r.chunk_ids]
            for i in range(0, len(mentions), 5000):
                await session.execute(insert(GraphMention), mentions[i:i + 5000])
        await self.store.replace_kb(kb_id, entities, relations)
        return len(entities), len(relations)

    async def _finish(self, build_id: uuid.UUID, status: GraphBuildStatus, **values) -> None:
        async with self.sessionmaker() as session, session.begin():
            build = await session.get(GraphBuild, build_id)
            build.status = status
            build.finished_at = func.now()
            build.cost_usd = round(self.prices.cost(build.input_tokens, build.cached_tokens, build.output_tokens), 6)
            for k, v in values.items():
                setattr(build, k, v)

    # ---------- 浏览与溯源 ----------

    async def overview(self, user_id: uuid.UUID, kb_id: uuid.UUID, limit: int = 60) -> dict:
        await self._owned_kb(user_id, kb_id)
        return await self.store.overview(kb_id, limit)

    async def search(self, user_id: uuid.UUID, kb_id: uuid.UUID, q: str, limit: int = 20) -> list[dict]:
        await self._owned_kb(user_id, kb_id)
        return await self.store.search(kb_id, q, limit)

    async def neighbors(self, user_id: uuid.UUID, entity_id: uuid.UUID, limit: int = 40) -> dict:
        await self._owned_entity(user_id, entity_id)
        return await self.store.neighbors(entity_id, limit)

    async def path(self, user_id: uuid.UUID, kb_id: uuid.UUID, source: uuid.UUID, target: uuid.UUID) -> dict | None:
        await self._owned_kb(user_id, kb_id)
        return await self.store.shortest_path(kb_id, source, target)

    async def entity_detail(self, user_id: uuid.UUID, entity_id: uuid.UUID, *, mention_limit: int = 20) -> dict:
        """实体详情：描述、关系（含对端名称）、出处原文。全部来自 PostgreSQL（事实来源）。"""
        entity = await self._owned_entity(user_id, entity_id)
        async with self.sessionmaker() as session:
            other = GraphEntity.__table__.alias("other")
            rels = []
            for col, other_col, direction in ((GraphRelation.source_id, GraphRelation.target_id, "out"),
                                               (GraphRelation.target_id, GraphRelation.source_id, "in")):
                stmt = (select(GraphRelation, other.c.id, other.c.name)
                        .join(other, other.c.id == other_col).where(col == entity_id)
                        .order_by(GraphRelation.mention_count.desc()).limit(50))
                for r, oid, oname in (await session.execute(stmt)).all():
                    rels.append({"id": r.id, "direction": direction, "predicate": r.predicate, "description": r.description,
                                 "other_id": oid, "other_name": oname, "mentions": r.mention_count})
            stmt = (select(DocumentChunk.id, DocumentChunk.text, DocumentChunk.page, DocumentChunk.seq, Document.filename)
                    .join(GraphMention, GraphMention.chunk_id == DocumentChunk.id)
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(GraphMention.entity_id == entity_id)
                    .order_by(Document.filename, DocumentChunk.seq).limit(mention_limit))
            mentions = [{"chunk_id": cid, "text": text, "page": page, "seq": seq, "filename": fn}
                        for cid, text, page, seq, fn in (await session.execute(stmt)).all()]
        return {"id": entity.id, "knowledge_base_id": entity.knowledge_base_id, "name": entity.name, "type": entity.type,
                "description": entity.description, "mention_count": entity.mention_count,
                "relations": rels, "mentions": mentions}

    # ---------- 内部 ----------

    async def _owned_kb(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        async with self.sessionmaker() as session:
            kb = await session.get(KnowledgeBase, kb_id)
        if kb is None or kb.user_id != user_id:
            raise KnowledgeBaseNotFound(str(kb_id))

    async def _owned_entity(self, user_id: uuid.UUID, entity_id: uuid.UUID) -> GraphEntity:
        async with self.sessionmaker() as session:
            entity = await session.get(GraphEntity, entity_id)
            kb = await session.get(KnowledgeBase, entity.knowledge_base_id) if entity else None
        if entity is None or kb is None or kb.user_id != user_id:
            raise EntityNotFound(str(entity_id))
        return entity

    async def _chunks(self, kb_id: uuid.UUID) -> list[_Chunk]:
        async with self.sessionmaker() as session:
            stmt = (select(DocumentChunk.id, DocumentChunk.text, Document.filename)
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(DocumentChunk.knowledge_base_id == kb_id, Document.status == DocumentStatus.READY)
                    .order_by(Document.created_at, DocumentChunk.seq))
            return [_Chunk(cid, text, fn) for cid, text, fn in (await session.execute(stmt)).all()]

    async def _cached(self, kb_id: uuid.UUID, hashes: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        async with self.sessionmaker() as session:
            for i in range(0, len(hashes), IN_BATCH):
                stmt = select(GraphExtraction.text_sha256, GraphExtraction.payload).where(
                    GraphExtraction.knowledge_base_id == kb_id, GraphExtraction.prompt_version == PROMPT_VERSION,
                    GraphExtraction.text_sha256.in_(hashes[i:i + IN_BATCH]))
                out.update((h, p) for h, p in (await session.execute(stmt)).all())
        return out


def _group(chunks: list[_Chunk]) -> dict[str, list[_Chunk]]:
    """按文本内容去重：内容完全相同的分块只抽一次（2.2 测试用的 3.3MB 文件，4667 块里绝大多数是重复的）。"""
    groups: dict[str, list[_Chunk]] = defaultdict(list)
    for c in chunks:
        groups[hashlib.sha256(c.text.encode()).hexdigest()].append(c)
    return dict(groups)


def _error_code(exc: BaseException) -> str:
    from neo4j.exceptions import Neo4jError, ServiceUnavailable

    if isinstance(exc, BaseExceptionGroup):  # TaskGroup 把子任务的异常包成异常组，取第一个
        exc = exc.exceptions[0]
    if isinstance(exc, GraphBuildTooLarge):
        return "graph_build_too_large"
    if isinstance(exc, (ServiceUnavailable, Neo4jError)):
        return "graph_store_unavailable"
    return str(classify(exc))
