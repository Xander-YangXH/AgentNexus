"""混合检索（阶段 7.5，decisions/0029）：关键词稀疏向量的切词与权重、与稠密向量的融合、知识库过滤、旧集合的退化与重建。

稠密向量用手工构造的向量（不加载模型），这样可以精确地造出“稠密召回漏掉、关键词能找回”的情形。
"""

import uuid

import pytest
from qdrant_client import AsyncQdrantClient, models

from agentnexus.knowledge import keywords
from agentnexus.knowledge.reindex import rebuild
from agentnexus.storage.vectors import SPARSE, ChunkIndex, ChunkPoint
from tests.test_ingest import _doc, svc  # noqa: F401 —— 复用入库测试的夹具
from tests.test_tenancy import role_sessionmaker, role_urls  # noqa: F401 —— 复用数据库角色的夹具

DIM = 8


def _vec(*head):
    v = [0.0] * DIM
    for i, x in enumerate(head):
        v[i] = x
    return v


# ---------- 切词与权重 ----------

def test_tokens_use_char_bigrams_for_chinese_and_whole_words_for_latin_and_digits():
    assert keywords.tokens("退货政策") == ["退货", "货政", "政策"]
    assert keywords.tokens("API 返回 429") == ["api", "返回", "429"]
    assert keywords.tokens("ＣＳＶ文件") == ["csv", "文件"]  # 全角转半角、转小写
    assert keywords.tokens("22:00 到 9:00") == ["22", "00", "到", "9", "00"]
    assert keywords.tokens("，。！") == []


def test_document_weights_saturate_and_query_weights_are_flat():
    once = keywords.document_vector("退货")
    many = keywords.document_vector("退货 " * 5)
    w1, w5 = once.values[0], many.values[0]
    assert w5 > w1 and w5 < keywords.K1 + 1  # 词频越高权重越大，但有上限（BM25 的饱和）
    q = keywords.query_vector("退货 退货 政策")  # 重复的词只算一次
    assert len(q.indices) == 2 and q.values == [1.0, 1.0]
    assert q.indices == sorted(q.indices)


# ---------- 索引与检索 ----------

@pytest.fixture
async def index():
    client = AsyncQdrantClient(location=":memory:")
    idx = ChunkIndex(client, "chunks", DIM)
    await idx.ensure()
    yield idx
    await client.close()


async def _fill(index, kb, *, n_near=11):
    """11 个稠密向量上离查询很近、但不含关键词的分块，加一个稠密向量很远、但包含“429”的分块。"""
    target = ChunkPoint(uuid.uuid4(), kb, uuid.uuid4(), _vec(0, 1), "接口每分钟最多调用 600 次，超出返回 429 错误。")
    near = [ChunkPoint(uuid.uuid4(), kb, uuid.uuid4(), _vec(1, 0.01 * i), f"登录与会话的说明第 {i} 段。")
            for i in range(n_near)]
    await index.upsert([target, *near])
    return target


async def test_keywords_bring_back_a_chunk_that_vector_search_alone_misses(index):
    kb = uuid.uuid4()
    target = await _fill(index, kb)
    dense_only = [c for c, _ in await index.search(_vec(1), [kb], limit=10)]
    hybrid = [c for c, _ in await index.search(_vec(1), [kb], limit=10, text="429")]
    assert target.chunk_id not in dense_only  # 稠密召回的前 10 个里没有它
    assert target.chunk_id in hybrid and len(hybrid) == 10


async def test_keyword_branch_respects_the_knowledge_base_filter(index):
    mine, other = uuid.uuid4(), uuid.uuid4()
    await _fill(index, mine)
    secret = ChunkPoint(uuid.uuid4(), other, uuid.uuid4(), _vec(0, 0, 1), "别的知识库：429 限流说明。")
    await index.upsert([secret])
    hits = [c for c, _ in await index.search(_vec(1), [mine], limit=20, text="429 限流")]
    assert secret.chunk_id not in hits  # 两路都带知识库过滤：关键词命中也拿不到别人的分块


async def test_queries_without_keywords_fall_back_to_vector_search(index):
    kb = uuid.uuid4()
    await _fill(index, kb)
    ids = lambda hits: [c for c, _ in hits]  # noqa: E731
    assert ids(await index.search(_vec(1), [kb], limit=3, text="，。？")) == ids(await index.search(_vec(1), [kb], limit=3))


async def test_an_old_collection_without_keyword_vectors_still_works_with_vectors_only():
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection("old", vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE))
    await client.update_collection_aliases(change_aliases_operations=[
        models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name="old", alias_name="chunks"))])
    index = ChunkIndex(client, "chunks", DIM)
    await index.ensure()
    assert not await index.hybrid()
    kb = uuid.uuid4()
    target = await _fill(index, kb)  # 写入时不带稀疏向量（旧集合不支持），不报错
    hits = [c for c, _ in await index.search(_vec(0, 1), [kb], limit=3, text="429")]
    assert hits[0] == target.chunk_id
    await client.close()


async def test_rebuilding_the_index_upgrades_it_to_hybrid(svc, user):  # noqa: F811
    client = svc.index.client  # 把夹具的集合换成“旧集合”（没有稀疏向量），再重建
    old = await svc.index.current_collection()
    await client.delete_collection(old)
    await client.create_collection("legacy", vectors_config=models.VectorParams(size=svc.index.dim,
                                                                                  distance=models.Distance.COSINE))
    await svc.index.switch_alias("legacy")
    svc.index._hybrid = None
    assert not await svc.index.hybrid()
    doc = await _doc(svc, user, text="开放平台接口超出频率会返回 429 错误。", name="api.txt")
    assert await svc.process(doc.id)

    await rebuild(svc.sessionmaker, svc.embedder, svc.index)
    info = await client.get_collection(svc.index.alias)
    assert SPARSE in (info.config.params.sparse_vectors or {})
    svc.index._hybrid_checked = 0.0  # 不等一分钟的自动复查
    svc.index._hybrid = None
    assert await svc.index.hybrid()
    points, _ = await client.scroll(svc.index.alias, limit=10, with_vectors=True)
    assert points and all(SPARSE in p.vector for p in points)  # 重建时写入了关键词向量


# ---------- 重建索引的两道保护（阶段 7.5 踩坑后补上） ----------

async def test_rebuild_refuses_to_replace_an_index_with_an_empty_one(svc, user, monkeypatch):  # noqa: F811
    from agentnexus.knowledge import reindex

    doc = await _doc(svc, user, name="a.txt")
    assert await svc.process(doc.id)
    before = await svc.index.current_collection()

    async def sees_nothing(*args, **kwargs):  # 例如 api 的角色受 RLS 约束，一行分块都读不到
        return 0

    monkeypatch.setattr(reindex, "_fill", sees_nothing)
    with pytest.raises(reindex.EmptyRebuild):
        await rebuild(svc.sessionmaker, svc.embedder, svc.index)
    assert await svc.index.current_collection() == before  # 别名没动、旧索引还在
    assert await svc.index.count(document_id=doc.id) > 0
    assert [c.name for c in (await svc.index.client.get_collections()).collections] == [before]  # 半成品已删


async def test_reindex_refuses_a_database_role_that_row_level_security_restricts(sessionmaker, role_sessionmaker):  # noqa: F811
    from agentnexus.knowledge.reindex import check_role

    await check_role(sessionmaker)  # 测试库的所有者
    await check_role(role_sessionmaker("worker"))  # BYPASSRLS
    with pytest.raises(SystemExit, match="worker container"):
        await check_role(role_sessionmaker("api"))
