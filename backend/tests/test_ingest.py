"""入库流水线（阶段 2.2）：向量化、写入索引、后台派发、启动恢复、失败重试、重建索引。"""

import asyncio
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient

from agentnexus.knowledge.reindex import rebuild
from agentnexus.repositories.knowledge import ChunkRepository, DocumentRepository
from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.postgres import DocumentStatus
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint, IndexMismatch
from server.deps import get_sessionmaker
from server.main import create_app
from tests.fakes import FakeEmbedder

TEXT = "退货政策：签收后 7 天内可以无理由退货。定制商品不支持退货。\n\n退款到账：入库检验后 1 到 3 个工作日原路退回。"


@pytest_asyncio.fixture
async def svc(sessionmaker, tmp_path, chunk_index):
    return KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index)


async def _doc(svc, user, text=TEXT, name="政策.txt", kb_name="kb"):
    kbs = {k.name: k for k in await svc.list_kbs(user.id)}
    kb = kbs.get(kb_name) or await svc.create_kb(user.id, kb_name, None)
    return await svc.upload(user.id, kb.id, name, text.encode())


async def _status(svc, document_id):
    async with svc.sessionmaker() as session:
        return (await DocumentRepository(session).get(document_id)).status


async def _point_ids(index, document_id):
    points, _ = await index.client.scroll(index.alias, limit=1000, with_payload=True)
    return {p.id: p.payload for p in points if p.payload["document_id"] == str(document_id)}


# ---------- 处理：向量写入索引 ----------

async def test_process_indexes_every_chunk_with_its_id_and_tenant(svc, user):
    doc = await _doc(svc, user)
    assert await svc.process(doc.id)
    async with svc.sessionmaker() as session:
        chunks = await ChunkRepository(session).list_by_document(doc.id)
    points = await _point_ids(svc.index, doc.id)
    # 点的 id 就是分块 id：检索结果拿 id 回数据库取原文
    assert set(points) == {str(c.id) for c in chunks} and chunks
    assert all(p == {"knowledge_base_id": str(doc.knowledge_base_id), "document_id": str(doc.id)} for p in points.values())


async def test_index_failure_marks_failed_without_chunks_or_points_then_reprocess_succeeds(svc, user, monkeypatch):
    doc = await _doc(svc, user)
    original = ChunkIndex.upsert

    async def broken(self, points, **kw):
        await original(self, points[:1], **kw)  # 写进去一部分再失败：失败路径要把它们清掉
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(ChunkIndex, "upsert", broken)
    assert await svc.process(doc.id)
    async with svc.sessionmaker() as session:
        got = await DocumentRepository(session).get(doc.id)
        assert (got.status, got.error_code) == (DocumentStatus.FAILED, "index_unavailable")
        assert await ChunkRepository(session).list_by_document(doc.id) == []
    assert await svc.index.count(document_id=doc.id) == 0

    monkeypatch.setattr(ChunkIndex, "upsert", original)  # 向量库恢复后，手动重新处理
    assert (await svc.reprocess(user.id, doc.id)).status == DocumentStatus.PENDING
    assert await svc.process(doc.id)
    assert await _status(svc, doc.id) == DocumentStatus.READY
    assert await svc.index.count(document_id=doc.id) == (await svc.get_document(user.id, doc.id)).chunk_count


async def test_points_are_removed_if_the_document_cannot_be_marked_ready(svc, user, monkeypatch):
    doc = await _doc(svc, user)
    original = DocumentRepository.finish

    async def refuse_ready(self, document_id, status, **kw):
        return False if status == DocumentStatus.READY else await original(self, document_id, status, **kw)

    monkeypatch.setattr(DocumentRepository, "finish", refuse_ready)
    assert await svc.process(doc.id) is False
    assert await svc.index.count(document_id=doc.id) == 0


async def test_interrupted_document_is_requeued_and_stale_points_are_replaced(svc, user):
    """进程在“写完索引、还没提交 ready”时被杀：启动恢复改回 pending，重做时先清掉上次留下的点。"""
    doc = await _doc(svc, user)
    async with svc.sessionmaker() as session, session.begin():
        assert await DocumentRepository(session).claim(doc.id)
    stale = uuid.uuid4()
    await svc.index.upsert([ChunkPoint(stale, doc.knowledge_base_id, doc.id, FakeEmbedder().embed_query("旧"))])

    assert await svc.requeue_interrupted() == [doc.id]
    assert await svc.process(doc.id)
    points = await _point_ids(svc.index, doc.id)
    assert str(stale) not in points
    assert len(points) == (await svc.get_document(user.id, doc.id)).chunk_count


async def test_only_failed_documents_can_be_reprocessed(svc, user):
    from agentnexus.services.errors import DocumentNotRetryable

    doc = await _doc(svc, user)
    with pytest.raises(DocumentNotRetryable):  # pending
        await svc.reprocess(user.id, doc.id)
    await svc.process(doc.id)
    with pytest.raises(DocumentNotRetryable):  # ready
        await svc.reprocess(user.id, doc.id)


# ---------- 后台派发器 ----------

async def _wait_status(svc, document_id, status, timeout=5.0):
    async def poll():
        while await _status(svc, document_id) != status:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


async def test_dispatcher_recovers_processing_and_sweeps_pending_on_start(svc, user):
    interrupted = await _doc(svc, user, name="a.txt")
    never_enqueued = await _doc(svc, user, text=TEXT + "补充说明。", name="b.txt")
    async with svc.sessionmaker() as session, session.begin():
        await DocumentRepository(session).claim(interrupted.id)

    dispatcher = JobDispatcher(svc, name="ingest", sweep_interval=60)
    await dispatcher.start()
    try:
        await _wait_status(svc, interrupted.id, DocumentStatus.READY)
        await _wait_status(svc, never_enqueued.id, DocumentStatus.READY)
    finally:
        await dispatcher.stop()


async def test_upload_through_api_is_processed_in_background(sessionmaker, svc):
    dispatcher = JobDispatcher(svc, name="ingest", sweep_interval=60)
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.state.knowledge_service = svc
    app.state.ingest_dispatcher = dispatcher
    await dispatcher.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = {"X-User-Id": (await c.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]}
            kb = (await c.post("/api/knowledge-bases", json={"name": "kb"}, headers=h)).json()["id"]
            doc = (await c.post(f"/api/knowledge-bases/{kb}/documents", files={"file": ("a.txt", TEXT.encode())}, headers=h)).json()
            await _wait_status(svc, uuid.UUID(doc["id"]), DocumentStatus.READY)

            bad = (await c.post(f"/api/knowledge-bases/{kb}/documents", files={"file": ("b.pdf", b"not a pdf")}, headers=h)).json()
            await _wait_status(svc, uuid.UUID(bad["id"]), DocumentStatus.FAILED)
            r = await c.post(f"/api/documents/{bad['id']}/reprocess", headers=h)
            assert r.status_code == 202 and r.json()["status"] == "pending"
            await _wait_status(svc, uuid.UUID(bad["id"]), DocumentStatus.FAILED)  # 文件本身坏了，重试还是失败
            r = await c.post(f"/api/documents/{doc['id']}/reprocess", headers=h)
            assert (r.status_code, r.json()["error"]["code"]) == (409, "document_not_retryable")
    finally:
        await dispatcher.stop()


# ---------- 索引管理 ----------

async def test_ensure_rejects_a_collection_built_for_another_model():
    client = AsyncQdrantClient(location=":memory:")
    await ChunkIndex(client, "chunks", 64).ensure()
    await ChunkIndex(client, "chunks", 64).ensure()  # 再次启动：已存在，不重建
    with pytest.raises(IndexMismatch):
        await ChunkIndex(client, "chunks", 512).ensure()
    await client.close()


async def test_rebuild_recreates_points_for_ready_documents_only_and_switches_alias(svc, user):
    ready = await _doc(svc, user, name="a.txt")
    await svc.process(ready.id)
    broken = await _doc(svc, user, text="x", name="b.pdf")
    await svc.process(broken.id)  # failed：不应进索引
    pending = await _doc(svc, user, text=TEXT + "还没处理。", name="c.txt")
    expected = await _point_ids(svc.index, ready.id)

    old = await svc.index.current_collection()
    # 模拟索引丢数据
    await svc.index.delete_document(ready.id)
    assert await svc.index.count() == 0

    n = await rebuild(svc.sessionmaker, svc.embedder, svc.index)
    assert n == len(expected)
    assert await svc.index.current_collection() != old
    assert not await svc.index.client.collection_exists(old)  # 旧集合已删除
    assert set(await _point_ids(svc.index, ready.id)) == set(expected)
    assert await svc.index.count() == n
    assert pending.id  # 未处理的文档等后台处理，不由重建负责


async def test_rebuild_catches_up_documents_finished_while_it_was_running(svc, user, monkeypatch):
    """重建期间完成的文档写进的是旧集合；切换别名后要补录进新集合，否则它会从检索里消失。"""
    first = await _doc(svc, user, name="a.txt")
    await svc.process(first.id)
    late = await _doc(svc, user, text=TEXT + "重建期间才处理完。", name="b.txt")
    original = ChunkIndex.switch_alias

    async def finish_another_then_switch(self, collection):
        # 第一遍填充已经结束、别名还没切：此时完成的文档经别名写进的是旧集合
        assert await svc.process(late.id)
        return await original(self, collection)

    monkeypatch.setattr(ChunkIndex, "switch_alias", finish_another_then_switch)
    await rebuild(svc.sessionmaker, svc.embedder, svc.index)
    assert await svc.index.count(document_id=late.id) == (await svc.get_document(user.id, late.id)).chunk_count > 0


async def test_rebuild_failure_keeps_the_old_index(svc, user, monkeypatch):
    doc = await _doc(svc, user)
    await svc.process(doc.id)
    old = await svc.index.current_collection()

    def boom(texts):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(svc.embedder, "embed_documents", boom)
    with pytest.raises(RuntimeError):
        await rebuild(svc.sessionmaker, svc.embedder, svc.index)
    assert await svc.index.current_collection() == old
    assert [c.name for c in (await svc.index.client.get_collections()).collections] == [old]  # 半成品已删
    assert await svc.index.count(document_id=doc.id) > 0


# ---------- 真实模型（本机已有模型缓存时才跑；不联网） ----------

@pytest.fixture(scope="module")
def real_embedder():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from agentnexus.knowledge.embedding import FastEmbedder

        return FastEmbedder()
    except Exception as exc:  # 没有模型缓存（例如 CI）
        pytest.skip(f"bge-small-zh model not available offline: {type(exc).__name__}")


def test_tokenizer_counter_is_not_truncated_and_matches_the_model(real_embedder):
    counter = real_embedder.token_counter()
    assert counter.count("长" * 600) == 600  # fastembed 内部那份分词器会截断到 512
    assert counter.count("退货政策") == 4  # BERT 中文：每个字一个 token


def test_real_embedding_ranks_the_relevant_chunk_first(real_embedder):
    docs = ["签收后 7 天内可以无理由退货，定制商品除外。", "会员积分每消费 1 元累计 1 分。", "发票可以在订单详情页申请电子发票。"]
    vecs = real_embedder.embed_documents(docs)
    q = real_embedder.embed_query("买的东西不想要了怎么办")
    scores = [sum(a * b for a, b in zip(q, v)) for v in vecs]
    assert scores.index(max(scores)) == 0


async def test_only_markdown_files_are_split_by_headings(svc, user):
    text = "## 退货\n签收后七天内可以退货。\n## 发票\n订单完成后申请电子发票。"
    md = await _doc(svc, user, text=text, name="手册.md")
    txt = await _doc(svc, user, text=text + "\n", name="手册.txt")  # 内容略不同，避免被当成重复文件
    for d in (md, txt):
        await svc.process(d.id)
    assert (await svc.get_document(user.id, md.id)).chunk_count == 2
    assert (await svc.get_document(user.id, txt.id)).chunk_count == 1
