"""分块向量索引（Qdrant，decisions/0014）。

Qdrant 只是派生索引：分块原文与状态以 PostgreSQL 为准，索引丢了可以用 `python -m agentnexus.knowledge.reindex` 重建。
- 一个集合放所有知识库，按 payload 里的 knowledge_base_id 过滤（Qdrant 推荐的多租户做法；
  is_tenant 让它按租户组织存储，单个知识库的检索只读自己的数据）。
- 应用只通过别名访问（默认 chunks）。重建时写进一个新集合，填完再原子地把别名切过去，检索不中断。
- 点的 id 就是分块 id（PostgreSQL 主键），检索结果拿 id 回数据库取原文。
"""

import dataclasses
import datetime as dt
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models

UPSERT_BATCH = 256


class IndexMismatch(RuntimeError):
    """现有集合的向量维度与当前模型不一致（换了模型）：需要重建索引。"""


@dataclasses.dataclass(frozen=True)
class ChunkPoint:
    chunk_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_id: uuid.UUID
    vector: list[float]


class ChunkIndex:
    def __init__(self, client: AsyncQdrantClient, alias: str, dim: int):
        self.client = client
        self.alias = alias
        self.dim = dim

    async def ensure(self) -> None:
        """启动时调用：别名不存在就建集合并挂上别名；存在则检查维度。"""
        if not await self.client.collection_exists(self.alias):
            await self.switch_alias(await self.create_collection())
            return
        info = await self.client.get_collection(self.alias)
        size = info.config.params.vectors.size
        if size != self.dim:
            raise IndexMismatch(
                f"collection behind alias {self.alias!r} has dim {size}, model has {self.dim}; "
                "run `python -m agentnexus.knowledge.reindex`"
            )

    async def create_collection(self) -> str:
        name = f"{self.alias}_{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S%f}"
        await self.client.create_collection(
            name, vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE)
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
        for i in range(0, len(points), UPSERT_BATCH):
            batch = points[i:i + UPSERT_BATCH]
            await self.client.upsert(
                target,
                points=[
                    models.PointStruct(
                        id=str(p.chunk_id), vector=p.vector,
                        payload={"knowledge_base_id": str(p.knowledge_base_id), "document_id": str(p.document_id)},
                    )
                    for p in batch
                ],
                wait=True,
            )

    async def delete_document(self, document_id: uuid.UUID) -> None:
        await self.client.delete(
            self.alias,
            points_selector=models.FilterSelector(filter=_match("document_id", document_id)),
            wait=True,
        )

    async def count(self, *, document_id: uuid.UUID | None = None) -> int:
        flt = _match("document_id", document_id) if document_id else None
        return (await self.client.count(self.alias, count_filter=flt, exact=True)).count


def _match(key: str, value) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=str(value)))])
