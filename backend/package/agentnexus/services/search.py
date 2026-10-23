"""知识库检索（阶段 2.3，decisions/0017）：向量召回 → 回数据库取原文 → 重排。

- 召回：问题转成向量，在 Qdrant 里按知识库过滤找最相近的分块；
- 回库：用点的 id（= 分块主键）从 PostgreSQL 取原文，只保留 ready 文档的分块。
  索引里可能有数据库已经没有的点（处理失败留下的、重建中的），这一步把它们自然过滤掉（0016）；
- 重排：cross-encoder 对候选逐个打分。开启重排时多召回一些（candidates 个）给它挑，否则直接取 top_k。
"""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.embedding import Embedder
from agentnexus.knowledge.hits import SearchHit
from agentnexus.knowledge.rerank import Reranker
from agentnexus.services.errors import KnowledgeBaseNotFound
from agentnexus.security import access
from agentnexus.storage.postgres import Document, DocumentChunk, DocumentStatus, UsageKind
from agentnexus.storage.vectors import ChunkIndex


@dataclasses.dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    reranked: bool
    timings_ms: dict[str, float]  # 各步耗时：embed / vector / fetch / rerank，测试页展示、调参用


class SearchService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        embedder: Embedder,
        index: ChunkIndex,
        reranker: Reranker | None,
        *,
        candidates: int = 10,
    ):
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.index = index
        self.reranker = reranker  # None：部署时关闭了重排（不加载模型，省约 0.8GB 内存）
        self.candidates = candidates
        self.usage = None  # UsageRecorder：装配时设置（decisions/0023）

    async def search(
        self,
        user_id: uuid.UUID,
        knowledge_base_ids: Sequence[uuid.UUID],
        query: str,
        *,
        top_k: int = 5,
        rerank: bool = True,
        source: str | None = None,
    ) -> SearchResult:
        kb_ids = list(dict.fromkeys(knowledge_base_ids))  # 去重并保持顺序
        await self._check_owned(user_id, kb_ids)
        if self.usage is not None and source:  # 检索次数（decisions/0023）
            await self.usage.record(user_id, UsageKind.SEARCH, source)
        rerank = rerank and self.reranker is not None
        timings: dict[str, float] = {}

        with _timer(timings, "embed"):
            vector = await asyncio.to_thread(self.embedder.embed_query, query)
        with _timer(timings, "vector"):
            scored = await self.index.search(vector, kb_ids, limit=max(top_k, self.candidates) if rerank else top_k)
        with _timer(timings, "fetch"):
            hits = await self._fetch(scored, kb_ids)
        if rerank and hits:
            with _timer(timings, "rerank"):
                scores = await asyncio.to_thread(self.reranker.score, query, [h.text for h in hits])
            hits = [dataclasses.replace(h, rerank_score=s) for h, s in zip(hits, scores)]
            hits.sort(key=lambda h: h.rerank_score, reverse=True)
        return SearchResult(hits=hits[:top_k], reranked=rerank, timings_ms=timings)

    async def _check_owned(self, user_id: uuid.UUID, kb_ids: list[uuid.UUID]) -> None:
        """自己的，或同部门共享的知识库（decisions/0023）。"""
        async with self.sessionmaker() as session:
            visible = set(await access.visible_kb_ids(session, user_id, kb_ids))
        missing = [k for k in kb_ids if k not in visible]
        if missing:
            raise KnowledgeBaseNotFound(str(missing[0]))  # 看不见的与不存在的一样返回 404

    async def _fetch(self, scored: list[tuple[uuid.UUID, float]], kb_ids: list[uuid.UUID]) -> list[SearchHit]:
        if not scored:
            return []
        stmt = (
            select(DocumentChunk, Document.filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id.in_([cid for cid, _ in scored]),
                Document.status == DocumentStatus.READY,
                # 数据库这边再按知识库过滤一次：不单靠索引里的 payload 做权限边界
                DocumentChunk.knowledge_base_id.in_(kb_ids),
            )
        )
        async with self.sessionmaker() as session:
            rows = {c.id: (c, filename) for c, filename in (await session.execute(stmt)).all()}
        hits = []
        for chunk_id, score in scored:  # 保持向量召回的顺序
            if chunk_id not in rows:
                continue  # 索引里有、数据库里没有（或文档不是 ready）：丢弃
            c, filename = rows[chunk_id]
            hits.append(SearchHit(
                chunk_id=c.id, document_id=c.document_id, knowledge_base_id=c.knowledge_base_id, filename=filename,
                seq=c.seq, page=c.page, text=c.text, vector_score=score, rerank_score=None,
            ))
        return hits


class _timer:
    def __init__(self, timings: dict[str, float], name: str):
        self.timings, self.name = timings, name

    def __enter__(self):
        self.start = time.perf_counter()

    def __exit__(self, *exc):
        self.timings[self.name] = round((time.perf_counter() - self.start) * 1000, 1)
