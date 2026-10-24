"""删除知识库与文档（阶段 7.1，decisions/0026）：数据库里立即删除，派生副本（Qdrant、原件、Neo4j）由清理任务删，
图谱按剩余的出处裁剪，失败退避重试，处理中的文档也能删。"""

import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from agentnexus.knowledge.graph.extraction import FakeGraphExtractor
from agentnexus.services.conversations import ConversationService
from agentnexus.services.errors import (
    ConversationNotFound,
    DocumentNotFound,
    GraphBuildInProgress,
    KnowledgeBaseNotFound,
    RunConflict,
)
from agentnexus.services.usage import usage_insert
from agentnexus.services.graph import GraphService
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.purge import PurgeService
from agentnexus.storage.blobs import BlobNotFound, LocalBlobStore
from agentnexus.storage.postgres import (
    AgentRun,
    Artifact,
    Conversation,
    ConversationKnowledgeBase,
    Document,
    DocumentChunk,
    GraphEntity,
    GraphExtraction,
    GraphMention,
    GraphRelation,
    KnowledgeBase,
    Message,
    PurgeJob,
    PurgeStatus,
    RunStatus,
    UsageEvent,
    UsageKind,
)
from agentnexus.storage.vectors import ChunkIndex
from tests.fakes import FakeEmbedder
from tests.test_graph import TEXT_A, TEXT_B, graph_store  # noqa: F401 —— 复用 Neo4j 夹具

OTHER = "发票：下单后 30 天内可以申请电子发票。\n\n专票需要提供税号和开户行。"


@pytest_asyncio.fixture
async def env(sessionmaker, tmp_path, chunk_index, user, graph_store):  # noqa: F811
    blobs = LocalBlobStore(tmp_path)
    knowledge = KnowledgeService(sessionmaker, blobs, FakeEmbedder(), chunk_index)
    purge = PurgeService(sessionmaker, chunk_index, blobs, graph_store, retry_base_seconds=60)
    kb = await knowledge.create_kb(user.id, f"kb-{uuid.uuid4().hex[:6]}", None)
    graph_store.created_kbs.append(kb.id)
    return knowledge, purge, kb, graph_store


async def _add(knowledge, user, kb, name, text):
    doc = await knowledge.upload(user.id, kb.id, name, text.encode())
    assert await knowledge.process(doc.id)
    return doc


async def _jobs(sessionmaker, kb_id) -> list[PurgeJob]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(PurgeJob).where(PurgeJob.knowledge_base_id == kb_id))).all())


async def _points(index: ChunkIndex, *, kb_id=None, document_id=None) -> int:
    points, _ = await index.client.scroll(index.alias, limit=10000, with_payload=True)
    return sum(1 for p in points if (kb_id is None or p.payload["knowledge_base_id"] == str(kb_id))
               and (document_id is None or p.payload["document_id"] == str(document_id)))


async def _blob_exists(blobs, key) -> bool:
    try:
        await blobs.get(key)
        return True
    except BlobNotFound:
        return False


# ---------- 文档 ----------

async def test_document_is_gone_from_the_database_at_once_and_from_derived_stores_after_purge(env, user, sessionmaker):
    knowledge, purge, kb, _ = env
    doc = await _add(knowledge, user, kb, "a.txt", OTHER)
    keep = await _add(knowledge, user, kb, "b.txt", "退货政策：签收后 7 天内可以无理由退货。")
    assert await _points(knowledge.index, document_id=doc.id) > 0

    await knowledge.delete_document(user.id, doc.id)
    # 事务提交后：数据库里已经没有了（列表、分块、检索回库都看不到）
    with pytest.raises(DocumentNotFound):
        await knowledge.get_document(user.id, doc.id)
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).where(DocumentChunk.document_id == doc.id)) == 0
    assert [d.id for d in await knowledge.list_documents(user.id, kb.id)] == [keep.id]
    # 派生副本还在，等清理任务
    [job] = await _jobs(sessionmaker, kb.id)
    assert (job.kind, job.document_id, job.blob_keys) == ("document", doc.id, [doc.blob_key])
    assert await _points(knowledge.index, document_id=doc.id) > 0

    assert await purge.process(job.id)
    assert await _points(knowledge.index, document_id=doc.id) == 0
    assert await _points(knowledge.index, document_id=keep.id) > 0  # 别的文档不受影响
    assert not await _blob_exists(knowledge.blobs, doc.blob_key)
    assert await _blob_exists(knowledge.blobs, keep.blob_key)
    assert await _jobs(sessionmaker, kb.id) == []  # 完成后任务删掉


async def test_failed_purge_backs_off_and_retries_until_it_succeeds(env, user, sessionmaker, monkeypatch):
    knowledge, purge, kb, _ = env
    doc = await _add(knowledge, user, kb, "a.txt", OTHER)
    await knowledge.delete_document(user.id, doc.id)
    [job] = await _jobs(sessionmaker, kb.id)

    original = ChunkIndex.delete_document

    async def down(self, document_id):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(ChunkIndex, "delete_document", down)
    assert await purge.process(job.id)
    async with sessionmaker() as s:
        failed = await s.get(PurgeJob, job.id)
        waiting = await s.scalar(select(PurgeJob.not_before > func.now()).where(PurgeJob.id == job.id))
    assert failed.status == PurgeStatus.PENDING and failed.attempts == 1 and "qdrant down" in failed.last_error
    assert waiting  # 退避期内不会被认领
    assert await purge.queue.claim(job.id, "w") is None

    monkeypatch.setattr(ChunkIndex, "delete_document", original)
    async with sessionmaker() as s, s.begin():  # 时间到了
        await s.execute(update(PurgeJob).where(PurgeJob.id == job.id).values(not_before=func.now()))
    assert await purge.process(job.id)
    assert await _jobs(sessionmaker, kb.id) == []
    assert await _points(knowledge.index, document_id=doc.id) == 0


async def test_a_document_deleted_while_it_is_being_processed_leaves_no_points(env, user, sessionmaker, monkeypatch):
    knowledge, purge, kb, _ = env
    doc = await knowledge.upload(user.id, kb.id, "a.txt", OTHER.encode())
    original = ChunkIndex.upsert

    async def delete_meanwhile(self, points, **kw):
        await knowledge.delete_document(user.id, doc.id)  # 处理到一半，用户删除了文档
        await original(self, points, **kw)

    monkeypatch.setattr(ChunkIndex, "upsert", delete_meanwhile)
    assert not await knowledge.process(doc.id)  # 结果被丢弃：行已经不在，带版本号的写回失败
    assert await _points(knowledge.index, document_id=doc.id) == 0  # 自己写的点自己删掉
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).where(DocumentChunk.document_id == doc.id)) == 0
    [job] = await _jobs(sessionmaker, kb.id)
    assert await purge.process(job.id)


async def test_a_document_deleted_right_after_it_was_claimed_is_skipped(env, user):
    knowledge, _, kb, _ = env
    doc = await knowledge.upload(user.id, kb.id, "a.txt", OTHER.encode())
    job = await knowledge.queue.claim(doc.id, "w")
    await knowledge.delete_document(user.id, doc.id)
    assert not await knowledge.execute(job)  # 不报错，什么也不做
    assert await _points(knowledge.index, document_id=doc.id) == 0


async def test_graph_is_pruned_to_what_the_remaining_documents_support(env, user, sessionmaker):
    knowledge, purge, kb, store = env
    a = await _add(knowledge, user, kb, "a.md", TEXT_A)
    b = await _add(knowledge, user, kb, "b.md", TEXT_B)
    graph = GraphService(sessionmaker, FakeGraphExtractor(), store)
    build = await graph.create_build(user.id, kb.id)
    assert await graph.process(build.id)

    async with sessionmaker() as s:
        b_chunks = set((await s.scalars(select(DocumentChunk.id).where(DocumentChunk.document_id == b.id))).all())
        b_texts = set((await s.scalars(select(DocumentChunk.text).where(DocumentChunk.document_id == b.id))).all())
        a_texts = set((await s.scalars(select(DocumentChunk.text).where(DocumentChunk.document_id == a.id))).all())
        mentions = (await s.execute(select(GraphMention.entity_id, GraphMention.chunk_id)
                                    .where(GraphMention.knowledge_base_id == kb.id, GraphMention.entity_id.is_not(None)))).all()
    by_entity: dict = {}
    for entity_id, chunk_id in mentions:
        by_entity.setdefault(entity_id, []).append(chunk_id)
    only_b = {e for e, cs in by_entity.items() if set(cs) <= b_chunks}
    expected = {e: sum(c not in b_chunks for c in cs) for e, cs in by_entity.items() if e not in only_b}
    assert only_b and expected  # 测试数据里既有只出自 b 的实体，也有 a、b 共有的

    await knowledge.delete_document(user.id, b.id)
    async with sessionmaker() as s:
        entities = {e.id: e.mention_count for e in (await s.scalars(
            select(GraphEntity).where(GraphEntity.knowledge_base_id == kb.id))).all()}
        relations = (await s.scalars(select(GraphRelation).where(GraphRelation.knowledge_base_id == kb.id))).all()
        rel_mentions = dict((await s.execute(select(GraphMention.relation_id, func.count()).where(
            GraphMention.knowledge_base_id == kb.id, GraphMention.relation_id.is_not(None))
            .group_by(GraphMention.relation_id))).all())
        cached = set((await s.scalars(select(GraphExtraction.text_sha256)
                                      .where(GraphExtraction.knowledge_base_id == kb.id))).all())
    assert entities == expected  # 只出自 b 的实体删掉，其余按剩下的出处重算提及次数
    assert all(rel_mentions.get(r.id) == r.mention_count for r in relations)  # 关系同样：没有出处的不留
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
    assert cached == {sha(t) for t in a_texts}  # 只出自 b 的抽取缓存也删掉（它是 b 的派生数据）
    assert not b_texts & a_texts

    # 清理任务按 PostgreSQL 重写 Neo4j；之后重新构建全部命中缓存，不花钱
    [job] = await _jobs(sessionmaker, kb.id)
    assert await purge.process(job.id)
    assert await store.count(kb.id) == (len(entities), len(relations))
    assert (await graph.estimate(user.id, kb.id))["extract_count"] == 0


async def test_a_relation_only_the_deleted_document_stated_is_removed_even_if_both_ends_remain(env, user, sessionmaker):
    # 假抽取器把相邻出现的词连成关系：a 里有 Qdrant–Redis、Redis–Kafka，b 里有 Qdrant–Kafka。
    # 删掉 b 之后两端的实体都还在（a 里提到了），但 Qdrant–Kafka 这条关系已经没有出处，必须删掉
    knowledge, _, kb, store = env
    await _add(knowledge, user, kb, "a.txt", "Qdrant Redis Kafka")
    b = await _add(knowledge, user, kb, "b.txt", "Qdrant Kafka")
    graph = GraphService(sessionmaker, FakeGraphExtractor(), store)
    assert await graph.process((await graph.create_build(user.id, kb.id)).id)

    async def edges():
        async with sessionmaker() as s:
            names = {e.id: e.name for e in (await s.scalars(
                select(GraphEntity).where(GraphEntity.knowledge_base_id == kb.id))).all()}
            rels = (await s.scalars(select(GraphRelation).where(GraphRelation.knowledge_base_id == kb.id))).all()
        return {(names[r.source_id], names[r.target_id]) for r in rels}

    assert ("Qdrant", "Kafka") in await edges()
    await knowledge.delete_document(user.id, b.id)
    assert await edges() == {("Qdrant", "Redis"), ("Redis", "Kafka")}


async def test_deleting_is_refused_while_a_graph_build_is_in_progress(env, user, sessionmaker):
    knowledge, _, kb, store = env
    doc = await _add(knowledge, user, kb, "a.md", TEXT_A)
    graph = GraphService(sessionmaker, FakeGraphExtractor(), store)
    build = await graph.create_build(user.id, kb.id)  # pending
    with pytest.raises(GraphBuildInProgress):
        await knowledge.delete_document(user.id, doc.id)
    with pytest.raises(GraphBuildInProgress):
        await knowledge.delete_kb(user.id, kb.id)
    assert await graph.process(build.id)
    await knowledge.delete_document(user.id, doc.id)  # 构建结束后可以删


# ---------- 知识库 ----------

async def test_knowledge_base_and_everything_in_it_is_deleted(env, user, sessionmaker):
    knowledge, purge, kb, store = env
    a = await _add(knowledge, user, kb, "a.md", TEXT_A)
    b = await _add(knowledge, user, kb, "b.md", TEXT_B)
    graph = GraphService(sessionmaker, FakeGraphExtractor(), store)
    assert await graph.process((await graph.create_build(user.id, kb.id)).id)
    other = await knowledge.create_kb(user.id, f"other-{uuid.uuid4().hex[:6]}", None)
    survivor = await _add(knowledge, user, other, "c.txt", OTHER)
    assert (await store.count(kb.id))[0] > 0

    await knowledge.delete_kb(user.id, kb.id)
    with pytest.raises(KnowledgeBaseNotFound):
        await knowledge.get_kb(user.id, kb.id)
    async with sessionmaker() as s:
        for model in (Document, DocumentChunk, GraphEntity, GraphRelation, GraphMention, GraphExtraction):
            assert await s.scalar(select(func.count()).select_from(model).where(model.knowledge_base_id == kb.id)) == 0
    [job] = await _jobs(sessionmaker, kb.id)
    assert sorted(job.blob_keys) == sorted([a.blob_key, b.blob_key])

    assert await purge.process(job.id)
    assert await _points(knowledge.index, kb_id=kb.id) == 0
    assert await store.count(kb.id) == (0, 0)
    assert not await _blob_exists(knowledge.blobs, a.blob_key)
    # 别的知识库完好
    assert await _points(knowledge.index, kb_id=other.id) > 0 and await _blob_exists(knowledge.blobs, survivor.blob_key)
    async with sessionmaker() as s:
        assert await s.get(KnowledgeBase, other.id) is not None


async def test_purge_is_idempotent(env, user, sessionmaker):
    """清理任务执行到一半崩溃、被接管后从头再做：每一步重复执行都没有问题。"""
    knowledge, purge, kb, _ = env
    doc = await _add(knowledge, user, kb, "a.txt", OTHER)
    await knowledge.delete_kb(user.id, kb.id)
    [job] = await _jobs(sessionmaker, kb.id)
    task = await _get(sessionmaker, job.id)
    await purge._purge(task)  # 第一次执行完了，但没来得及删任务（模拟崩溃）
    assert await purge.process(job.id)  # 再执行一遍
    assert await _jobs(sessionmaker, kb.id) == []
    assert not await _blob_exists(knowledge.blobs, doc.blob_key)


async def _get(sessionmaker, job_id):
    async with sessionmaker() as s:
        return await s.get(PurgeJob, job_id)


# ---------- 对话（阶段 7.3，decisions/0028） ----------

class FakeCheckpointer:
    def __init__(self):
        self.deleted = []

    async def adelete_thread(self, thread_id):
        self.deleted.append(thread_id)


class FakeSandbox:
    def __init__(self):
        self.deleted = []

    async def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)


async def _conversation(sessionmaker, user, kb_id, blobs, *, status=RunStatus.SUCCEEDED):
    """一个用过的对话：两条消息、两次 run（一次的 checkpoint 已被清理）、一个产物、关联一个知识库、一条用量。"""
    async with sessionmaker() as s, s.begin():
        conv = Conversation(user_id=user.id, title="要删除的对话")
        s.add(conv)
        await s.flush()
        q, a = Message(conversation_id=conv.id, role="user", content="问题"), Message(conversation_id=conv.id, role="assistant", content="回答")
        s.add_all([q, a])
        await s.flush()
        old = AgentRun(conversation_id=conv.id, input_message_id=q.id, status=RunStatus.SUCCEEDED,
                       checkpoint_purged_at=func.now())
        run = AgentRun(conversation_id=conv.id, input_message_id=q.id, output_message_id=a.id, status=status)
        s.add_all([old, run])
        await s.flush()
        artifact = Artifact(id=uuid.uuid4(), user_id=user.id, conversation_id=conv.id, run_id=run.id, filename="chart.png",
                            content_type="image/png", size=3, blob_key=f"artifacts/{uuid.uuid4()}")
        s.add(artifact)
        s.add(ConversationKnowledgeBase(conversation_id=conv.id, knowledge_base_id=kb_id))
        await s.execute(usage_insert(user.id, UsageKind.LLM, "chat", run_id=run.id, input_tokens=100, output_tokens=10))
    await blobs.put(artifact.blob_key, b"png")
    return conv, run, artifact


async def test_deleting_a_conversation_removes_its_data_and_queues_cleanup(env, user, sessionmaker):
    knowledge, purge, kb, _ = env
    conv, run, artifact = await _conversation(sessionmaker, user, kb.id, knowledge.blobs)
    convs = ConversationService(sessionmaker)

    await convs.delete(user.id, conv.id)
    with pytest.raises(ConversationNotFound):
        await convs.messages(user.id, conv.id)
    async with sessionmaker() as s:
        for model in (Message, AgentRun, Artifact, ConversationKnowledgeBase):
            assert await s.scalar(select(func.count()).select_from(model).where(model.conversation_id == conv.id)) == 0
        # 用量明细保留：只有数字、没有内容，删除对话不改变已经发生的费用
        assert await s.scalar(select(func.count()).where(UsageEvent.run_id == run.id)) == 1
        job = await s.scalar(select(PurgeJob).where(PurgeJob.conversation_id == conv.id))
    assert (job.kind, job.blob_keys, job.run_ids) == ("conversation", [artifact.blob_key], [str(run.id)])  # 已清理的 checkpoint 不再列出
    assert await knowledge.get_kb(user.id, kb.id)  # 关联的知识库本身不受影响

    purge.checkpointer, purge.sandbox = FakeCheckpointer(), FakeSandbox()
    assert await purge.process(job.id)
    assert purge.checkpointer.deleted == [str(run.id)] and purge.sandbox.deleted == [str(conv.id)]
    assert not await _blob_exists(knowledge.blobs, artifact.blob_key)
    async with sessionmaker() as s:
        assert await s.get(PurgeJob, job.id) is None


async def test_conversation_cleanup_waits_for_the_worker_parts(env, user, sessionmaker):
    """api 进程里没有 checkpointer：这类任务失败、退避重试，不会被当成“清理完了”。"""
    knowledge, purge, kb, _ = env
    conv, run, _ = await _conversation(sessionmaker, user, kb.id, knowledge.blobs)
    await ConversationService(sessionmaker).delete(user.id, conv.id)
    async with sessionmaker() as s:
        job = await s.scalar(select(PurgeJob).where(PurgeJob.conversation_id == conv.id))
    assert await purge.process(job.id)  # purge.checkpointer 为 None
    async with sessionmaker() as s:
        job = await s.get(PurgeJob, job.id)
    assert job.status == PurgeStatus.PENDING and "checkpointer" in job.last_error


async def test_a_conversation_with_an_active_run_or_of_someone_else_cannot_be_deleted(env, user, sessionmaker):
    knowledge, _, kb, _ = env
    convs = ConversationService(sessionmaker)
    for status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL):
        conv, _, _ = await _conversation(sessionmaker, user, kb.id, knowledge.blobs, status=status)
        with pytest.raises(RunConflict):
            await convs.delete(user.id, conv.id)
    with pytest.raises(ConversationNotFound):
        await convs.delete(uuid.uuid4(), conv.id)  # 不是自己的与不存在一样
    async with sessionmaker() as s:
        assert await s.get(Conversation, conv.id) is not None

