"""分块向量索引（Qdrant，decisions/0014）。

Qdrant 只是派生索引：分块原文与状态以 PostgreSQL 为准，索引丢了可以用 `python -m agentnexus.knowledge.reindex` 重建。
- 一个集合放所有知识库，按 payload 里的 knowledge_base_id 过滤（Qdrant 推荐的多租户做法；
  is_tenant 让它按租户组织存储，单个知识库的检索只读自己的数据）。
- 应用只通过别名访问（默认 chunks）。重建时写进一个新集合，填完再原子地把别名切过去，检索不中断。
- 点的 id 就是分块 id（PostgreSQL 主键），检索结果拿 id 回数据库取原文。
- 混合检索（阶段 7.5，decisions/0029）：每个点除了稠密向量，还有一个关键词稀疏向量（knowledge/keywords.py，
  IDF 由 Qdrant 在服务端计算）。检索时两路各取候选，在 Qdrant 里按排名融合（RRF），一次请求完成。
  旧集合没有稀疏向量时退回只用稠密向量，并提示重建索引；集合是否支持每分钟重新检查一次（重建后不用重启）。
"""

import dataclasses
import datetime as dt
import logging
import time
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models

from agentnexus.knowledge import keywords

log = logging.getLogger(__name__)

UPSERT_BATCH = 256
SPARSE = "text"  # 关键词稀疏向量的名字；稠密向量是默认（未命名）向量
HYBRID_RECHECK_SECONDS = 60


class IndexMismatch(RuntimeError):
    """现有集合的向量维度与当前模型不一致（换了模型）：需要重建索引。"""


@dataclasses.dataclass(frozen=True)
class ChunkPoint:
    chunk_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_id: uuid.UUID
    vector: list[float]
    text: str | None = None  # 分块原文：用来生成关键词稀疏向量


class ChunkIndex:
    def __init__(self, client: AsyncQdrantClient, alias: str, dim: int):
        self.client = client
        self.alias = alias
        self.dim = dim
        self._hybrid: bool | None = None
        self._hybrid_checked = 0.0

    async def hybrid(self) -> bool:
        """别名指向的集合是否有关键词稀疏向量（旧集合没有，重建索引后才有）。"""
        if self._hybrid is None or time.monotonic() - self._hybrid_checked > HYBRID_RECHECK_SECONDS:
            info = await self.client.get_collection(self.alias)
            supported = SPARSE in (info.config.params.sparse_vectors or {})
            if not supported and self._hybrid is not False:
                log.warning("collection behind %r has no keyword vectors; using vector search only. "
                            "Run `python -m agentnexus.knowledge.reindex` in the worker to enable hybrid search", self.alias)
            self._hybrid, self._hybrid_checked = supported, time.monotonic()
        return self._hybrid

    async def ensure(self) -> None:
        """启动时调用：别名不存在就建集合并挂上别名；存在则检查维度。"""
        if not await self.client.collection_exists(self.alias):
            await self.switch_alias(await self.create_collection())
            return
        info = await self.client.get_collection(self.alias)
        size = info.config.params.vectors.size
        await self.hybrid()
        if size != self.dim:
            raise IndexMismatch(
                f"collection behind alias {self.alias!r} has dim {size}, model has {self.dim}; "
                "run `python -m agentnexus.knowledge.reindex` in the worker"
            )

    async def create_collection(self) -> str:
        name = f"{self.alias}_{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S%f}"
        await self.client.create_collection(
            name, vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        await self.client.create_payload_index(
            name, "knowledge_base_id",
            models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
        )
        await self.client.create_payload_index(name, "document_id", models.PayloadSchemaType.KEYWORD)
        return name

    async def switch_alias(self, collection: str) -> str | None:
        """把别名原子地指向 collection，返回原来指向的集合（没有则 None）。"""
        old = await self.current_collection()
        ops: list = []
        if old is not None:
            ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=self.alias)))
        ops.append(models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=collection, alias_name=self.alias)))
        await self.client.update_collection_aliases(change_aliases_operations=ops)  # 一次请求里删旧建新：原子切换
        return old

    async def current_collection(self) -> str | None:
        for a in (await self.client.get_aliases()).aliases:
            if a.alias_name == self.alias:
                return a.collection_name
        return None

    async def drop(self, collection: str) -> None:
        await self.client.delete_collection(collection)

    async def upsert(self, points: Sequence[ChunkPoint], *, collection: str | None = None) -> None:
        # wait=True：返回时已写入，之后再提交数据库里的“ready”，保证 ready 的文档一定能被检索到
        target = collection or self.alias
        sparse = collection is not None or await self.hybrid()  # 新建的集合（重建索引时）一定支持
        for i in range(0, len(points), UPSERT_BATCH):
            batch = points[i:i + UPSERT_BATCH]
            await self.client.upsert(
                target,
                points=[
                    models.PointStruct(
                        id=str(p.chunk_id), vector=_vectors(p, sparse),
                        payload={"knowledge_base_id": str(p.knowledge_base_id), "document_id": str(p.document_id)},
                    )
                    for p in batch
                ],
                wait=True,
            )

    async def search(
        self, vector: list[float], knowledge_base_ids: Sequence[uuid.UUID], *, limit: int, text: str | None = None
    ) -> list[tuple[uuid.UUID, float]]:
        """在指定知识库范围内找最相关的分块，返回 [(分块 id, 分数)]，按分数从高到低。

        知识库过滤在 Qdrant 内部和检索一起做（不是先取近邻再过滤），所以过滤后的条数不会变少（0014 的基准）。
        给了 text 且集合支持时做混合检索：稠密、关键词两路各取 limit 个候选（都带知识库过滤），按排名融合（RRF）。
        融合后的分数是排名分（越大越靠前），不再是余弦相似度；线上后面还有重排，最终排序以重排为准。
        """
        flt = models.Filter(must=[models.FieldCondition(
            key="knowledge_base_id", match=models.MatchAny(any=[str(k) for k in knowledge_base_ids]))])
        query_sparse = keywords.query_vector(text) if text else None
        if query_sparse and query_sparse.indices and await self.hybrid():
            result = await self.client.query_points(
                self.alias,
                prefetch=[
                    models.Prefetch(query=vector, filter=flt, limit=limit),
                    models.Prefetch(query=models.SparseVector(indices=query_sparse.indices, values=query_sparse.values),
                                    using=SPARSE, filter=flt, limit=limit),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=False,
            )
        else:
            result = await self.client.query_points(self.alias, query=vector, query_filter=flt, limit=limit,
                                                    with_payload=False)
        return [(uuid.UUID(str(p.id)), p.score) for p in result.points]

    async def delete_document(self, document_id: uuid.UUID) -> None:
        await self.client.delete(
            self.alias,
            points_selector=models.FilterSelector(filter=_match("document_id", document_id)),
            wait=True,
        )

    async def delete_knowledge_base(self, kb_id: uuid.UUID) -> None:
        await self.client.delete(
            self.alias,
            points_selector=models.FilterSelector(filter=_match("knowledge_base_id", kb_id)),
            wait=True,
        )

    async def delete_points(self, ids: Sequence[uuid.UUID]) -> None:
        if ids:
            await self.client.delete(self.alias, points_selector=models.PointIdsList(points=[str(i) for i in ids]), wait=True)

    async def delete_document_except(self, document_id: uuid.UUID, keep: Sequence[uuid.UUID]) -> None:
        """删掉这个文档里“不属于本次结果”的点（之前中断的尝试、被接管的旧 worker 留下的）。

        只能由成功提交结果的执行者调用（decisions/0020）：按文档整体删除会误删接管者刚写好的点——
        fencing token 只能保护数据库里的写入，外部系统的删除要靠“只删别人的、只由赢家来删”。
        """
        await self.client.delete(
            self.alias,
            points_selector=models.FilterSelector(filter=models.Filter(
                must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=str(document_id)))],
                must_not=[models.HasIdCondition(has_id=[str(i) for i in keep])],
            )),
            wait=True,
        )

    async def count(self, *, document_id: uuid.UUID | None = None) -> int:
        flt = _match("document_id", document_id) if document_id else None
        return (await self.client.count(self.alias, count_filter=flt, exact=True)).count


def _vectors(p: ChunkPoint, sparse: bool):
    if not (sparse and p.text):
        return p.vector
    sv = keywords.document_vector(p.text)
    return {"": p.vector, SPARSE: models.SparseVector(indices=sv.indices, values=sv.values)}


def _match(key: str, value) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=str(value)))])
