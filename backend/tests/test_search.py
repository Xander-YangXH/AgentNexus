"""检索（阶段 2.3）：向量召回 + 回库取原文 + 重排；权限与孤儿点过滤。"""

import gc
import os
import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.services.errors import KnowledgeBaseNotFound
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint
from server.deps import get_sessionmaker
from server.main import create_app
from tests.fakes import NATIVE_MODELS, FakeEmbedder

DOCS = {
    "退货.txt": "退货政策：签收后七天内可以无理由退货，定制商品不支持退货。",
    "发票.txt": "发票说明：订单完成后可以在订单详情页申请电子发票。",
    "积分.txt": "会员积分：每消费一元累计一分，积分可以抵扣运费。",
}


class KeywordReranker:
    """假重排：包含关键词的片段得高分。用来确认结果确实按重排分数重新排序了。"""

    def __init__(self, keyword):
        self.keyword = keyword
        self.calls = []

    def score(self, query, texts):
        self.calls.append(list(texts))
        return [10.0 if self.keyword in t else -float(i) for i, t in enumerate(texts)]


@pytest_asyncio.fixture
async def kb_env(sessionmaker, tmp_path, chunk_index, user):
    knowledge = KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index)
    kb = await knowledge.create_kb(user.id, "客服", None)
    docs = {}
    for name, text in DOCS.items():
        doc = await knowledge.upload(user.id, kb.id, name, text.encode())
        assert await knowledge.process(doc.id)
        docs[name] = doc
    return knowledge, kb, docs


def _search(knowledge, reranker=None, candidates=10):
    return SearchService(knowledge.sessionmaker, knowledge.embedder, knowledge.index, reranker, candidates=candidates)


async def test_vector_search_returns_text_and_source_ordered_by_similarity(kb_env, user):
    knowledge, kb, docs = kb_env
    result = await _search(knowledge).search(user.id, [kb.id], "签收后七天内可以退货吗", top_k=3)
    assert not result.reranked and set(result.timings_ms) == {"embed", "vector", "fetch"}
    assert result.hits[0].filename == "退货.txt" and "无理由退货" in result.hits[0].text
    assert result.hits[0].document_id == docs["退货.txt"].id
    scores = [h.vector_score for h in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert all(h.rerank_score is None for h in result.hits)


async def test_rerank_reorders_candidates_by_rerank_score(kb_env, user):
    knowledge, kb, _ = kb_env
    reranker = KeywordReranker("积分")
    result = await _search(knowledge, reranker).search(user.id, [kb.id], "签收后七天内可以退货吗", top_k=2)
    assert result.reranked and "rerank" in result.timings_ms
    assert result.hits[0].filename == "积分.txt"  # 向量分数不是第一，但重排分数最高
    assert [h.rerank_score for h in result.hits] == sorted((h.rerank_score for h in result.hits), reverse=True)
    assert len(reranker.calls[0]) == 3  # 开启重排时召回的是全部候选（这里只有 3 块），不是只取 top_k 再排


async def test_candidate_count_depends_on_rerank(kb_env, user, monkeypatch):
    knowledge, kb, _ = kb_env
    limits = []
    original = ChunkIndex.search

    async def spy(self, vector, kb_ids, *, limit):
        limits.append(limit)
        return await original(self, vector, kb_ids, limit=limit)

    monkeypatch.setattr(ChunkIndex, "search", spy)
    await _search(knowledge, KeywordReranker("x"), candidates=8).search(user.id, [kb.id], "退货", top_k=2)
    await _search(knowledge, KeywordReranker("x"), candidates=8).search(user.id, [kb.id], "退货", top_k=2, rerank=False)
    await _search(knowledge, None, candidates=8).search(user.id, [kb.id], "退货", top_k=2)  # 部署关闭了重排
    assert limits == [8, 2, 2]


async def test_points_without_a_ready_chunk_in_the_database_are_dropped(kb_env, user):
    """索引只是派生数据：里面有、数据库里没有（或文档不是 ready）的点不能出现在结果里。"""
    knowledge, kb, docs = kb_env
    query = DOCS["退货.txt"]
    vec = FakeEmbedder().embed_query(query)
    orphan = uuid.uuid4()  # 数据库里根本没有这个分块（例如处理失败、没清掉的点）
    await knowledge.index.upsert([ChunkPoint(orphan, kb.id, docs["退货.txt"].id, vec)])
    # 文档已有分块和点、但状态不是 ready（正常流程不会出现，以后删除文档时可能先标记再清理）：也不能被检索到
    from sqlalchemy import update

    from agentnexus.storage.postgres import Document, DocumentStatus

    async with knowledge.sessionmaker() as session, session.begin():
        await session.execute(update(Document).where(Document.id == docs["发票.txt"].id).values(status=DocumentStatus.PROCESSING))
    result = await _search(knowledge).search(user.id, [kb.id], query, top_k=10)
    assert orphan not in {h.chunk_id for h in result.hits}
    assert {h.filename for h in result.hits} == {"退货.txt", "积分.txt"}


async def test_database_filters_by_knowledge_base_too(kb_env, user):
    """不单靠索引里的 payload 做权限边界：payload 写错了知识库，数据库这边也要拦住。"""
    knowledge, kb, docs = kb_env
    other = await knowledge.create_kb(user.id, "另一个库", None)
    async with knowledge.sessionmaker() as session:
        from agentnexus.repositories.knowledge import ChunkRepository

        chunk = (await ChunkRepository(session).list_by_document(docs["退货.txt"].id))[0]
    vec = FakeEmbedder().embed_query(chunk.text)
    await knowledge.index.upsert([ChunkPoint(chunk.id, other.id, chunk.document_id, vec)])  # payload 指向另一个库
    result = await _search(knowledge).search(user.id, [other.id], chunk.text, top_k=5)
    assert result.hits == []


async def test_only_own_knowledge_bases_can_be_searched(kb_env, sessionmaker, user):
    knowledge, kb, _ = kb_env
    from agentnexus.services.users import UserService

    bob = await UserService(sessionmaker).create(f"bob-{uuid.uuid4().hex[:6]}")
    search = _search(knowledge)
    with pytest.raises(KnowledgeBaseNotFound):
        await search.search(bob.id, [kb.id], "退货")
    bob_kb = await knowledge.create_kb(bob.id, "bob 的库", None)
    with pytest.raises(KnowledgeBaseNotFound):  # 混入一个别人的库也不行
        await search.search(bob.id, [bob_kb.id, kb.id], "退货")
    assert (await search.search(bob.id, [bob_kb.id], "退货")).hits == []  # 空库：没有结果，不报错


async def test_search_api(kb_env, sessionmaker, user):
    knowledge, kb, _ = kb_env
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.state.search_service = _search(knowledge, KeywordReranker("发票"))
    h = {"X-User-Id": str(user.id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/knowledge/search", json={"knowledge_base_ids": [str(kb.id)], "query": "怎么退货", "top_k": 2}, headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["reranked"] is True and len(body["hits"]) == 2 and body["hits"][0]["filename"] == "发票.txt"
        assert {"chunk_id", "document_id", "filename", "page", "seq", "text", "vector_score", "rerank_score"} <= set(body["hits"][0])
        for bad in ({"knowledge_base_ids": [], "query": "x"}, {"knowledge_base_ids": [str(kb.id)], "query": ""},
                    {"knowledge_base_ids": [str(kb.id)], "query": "x", "top_k": 21}):
            assert (await c.post("/api/knowledge/search", json=bad, headers=h)).status_code == 422
        r = await c.post("/api/knowledge/search", json={"knowledge_base_ids": [str(uuid.uuid4())], "query": "x"}, headers=h)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "knowledge_base_not_found")


# ---------- 真实重排模型（本机已有模型缓存时才跑；不联网） ----------

@pytest.fixture(scope="module")
def real_reranker():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from agentnexus.knowledge.rerank import FastReranker

        model = FastReranker()
        NATIVE_MODELS["loaded"] = True
    except Exception as exc:
        pytest.skip(f"reranker model not available offline: {type(exc).__name__}")
    yield model
    # 主动释放 ONNX 会话：留到解释器退出时再析构，偶发 “recursive_mutex lock failed” 导致进程以 134 退出（实测 4 次中 1 次）
    del model
    gc.collect()


def test_real_reranker_puts_the_answer_first(real_reranker):
    texts = ["会员积分每消费 1 元累计 1 分。", "签收后 7 天内可以无理由退货，定制商品除外。", "发票可以在订单详情页申请。"]
    scores = real_reranker.score("买的东西不想要了能退吗", texts)
    assert scores.index(max(scores)) == 1
