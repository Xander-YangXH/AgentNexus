"""删除知识库与文档（阶段 7.1，decisions/0026）。

一篇文档的数据在四个地方：PostgreSQL（文档、分块、图谱的实体 / 关系 / 出处、抽取缓存）是事实来源；
Qdrant 的点、原件文件、Neo4j 的图是派生副本。删除分两步：

1. 一个 PostgreSQL 事务（api 里同步完成）：删掉事实数据，裁剪图谱，同时插入一条清理任务（发件箱）。
   提交之后，所有读取路径（检索回库取原文、文档列表、图谱溯源）都已经看不到它；
2. worker 执行清理任务：删 Qdrant 的点、删原件、按 PostgreSQL 重写 Neo4j 里这个知识库的图。
   每一步都是幂等的，失败就退避重试，直到成功才删掉任务。

为什么不在请求里直接删四个地方：任何一个派生存储暂时不可用，要么删除失败（用户删不掉自己的数据），
要么静默留下残余（Qdrant 里没人引用的点还会占掉检索的候选名额）。发件箱让“删除成功”只取决于数据库事务，
“清理一定会发生”由任务保证。实验（eval/deletion_exp/scale.py）：10 万个分块的知识库在事务里删除约 1.2 秒，
所以第 1 步同步做是可以接受的。

删除对话（阶段 7.3，decisions/0028）走同一套：派生副本是产物原件、沙盒工作空间、LangGraph checkpoint。
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentnexus.knowledge.graph.store import Neo4jGraphStore
from agentnexus.runtime.lease import Claimed, LeaseQueue
from agentnexus.storage.blobs import BlobStore
from agentnexus.storage.postgres import (
    DocumentChunk,
    GraphBuild,
    GraphBuildStatus,
    GraphEntity,
    GraphExtraction,
    GraphMention,
    GraphRelation,
    KnowledgeBase,
    PurgeJob,
    PurgeKind,
    PurgeStatus,
)
from agentnexus.storage.vectors import ChunkIndex

log = logging.getLogger(__name__)

ACTIVE_BUILD = (GraphBuildStatus.PENDING, GraphBuildStatus.RUNNING)


async def graph_build_active(session: AsyncSession, kb_id: uuid.UUID) -> bool:
    stmt = select(exists().where(GraphBuild.knowledge_base_id == kb_id, GraphBuild.status.in_(ACTIVE_BUILD)))
    return bool(await session.scalar(stmt))


async def prune_graph(session: AsyncSession, kb_id: uuid.UUID) -> None:
    """删除文档后裁剪图谱（在删除文档的同一事务里）。

    分块被删时出处（kg_mentions）已经级联删掉了。这里再删掉“已经没有任何出处”的关系和实体、重算提及次数，
    并删掉不再对应任何分块的抽取缓存（缓存的是模型从这段文字里抽出来的内容，也是被删文档的派生数据）。
    同时出现在别的文档里的实体会保留，它的描述可能还带着被删文档的信息：重新构建图谱可以重算（没有新文档时
    全部命中缓存，不花钱）。
    """
    no_rel_mention = ~exists().where(GraphMention.relation_id == GraphRelation.id)
    await session.execute(delete(GraphRelation).where(GraphRelation.knowledge_base_id == kb_id, no_rel_mention))
    no_entity_mention = ~exists().where(GraphMention.entity_id == GraphEntity.id)
    await session.execute(delete(GraphEntity).where(GraphEntity.knowledge_base_id == kb_id, no_entity_mention))
    entity_mentions = select(func.count()).where(GraphMention.entity_id == GraphEntity.id).scalar_subquery()
    await session.execute(update(GraphEntity).where(GraphEntity.knowledge_base_id == kb_id)
                          .values(mention_count=entity_mentions))
    relation_mentions = select(func.count()).where(GraphMention.relation_id == GraphRelation.id).scalar_subquery()
    await session.execute(update(GraphRelation).where(GraphRelation.knowledge_base_id == kb_id)
                          .values(mention_count=relation_mentions))
    # 缓存的键是分块文本的 SHA-256（与 services/graph.py 的 _group 一致：UTF-8 编码后取十六进制）
    chunk_hash = func.encode(func.sha256(func.convert_to(DocumentChunk.text, text("'UTF8'"))), text("'hex'"))
    still_used = exists().where(DocumentChunk.knowledge_base_id == kb_id, chunk_hash == GraphExtraction.text_sha256)
    await session.execute(delete(GraphExtraction).where(GraphExtraction.knowledge_base_id == kb_id, ~still_used))


class PurgeService:
    """执行清理任务（worker 里）。

    sandbox、checkpointer 只在 worker 里有（assembly 在 worker 分支里设置）：对话的清理任务需要它们，
    缺了就失败、退避重试，不会“当作清理完了”。
    """

    def __init__(self, sessionmaker: async_sessionmaker, index: ChunkIndex, blobs: BlobStore,
                 graph_store: Neo4jGraphStore, *, lease_seconds: float = 30.0, retry_base_seconds: float = 5.0,
                 retry_max_seconds: float = 600.0, sandbox=None, checkpointer=None):
        self.sessionmaker = sessionmaker
        self.index = index
        self.blobs = blobs
        self.graph_store = graph_store
        self.sandbox = sandbox  # SandboxClient；没有部署沙盒时为 None（也就不会有工作空间）
        self.checkpointer = checkpointer
        self.retry_base = retry_base_seconds
        self.retry_max = retry_max_seconds
        self.queue = LeaseQueue(sessionmaker, PurgeJob, pending=PurgeStatus.PENDING, running=PurgeStatus.RUNNING,
                                lease_seconds=lease_seconds, notify_kind="purge", not_before=PurgeJob.not_before)

    async def process(self, job_id: uuid.UUID) -> bool:
        """认领并立即执行（同步调用：测试、命令行）。"""
        job = await self.queue.claim(job_id, "inline")
        return await self.execute(job) if job else False

    async def execute(self, job: Claimed) -> bool:
        async with self.sessionmaker() as session:
            task = await session.get(PurgeJob, job.id)
        try:
            await self._purge(task)
        except Exception as exc:
            # 退避重试：5 秒、10 秒、20 秒……最多 10 分钟。不设次数上限：删除必须最终完成
            delay = min(self.retry_base * 2 ** max(job.attempts - 1, 0), self.retry_max)
            log.warning("purge %s of %s failed (attempt %d), retrying in %.0fs", task.kind,
                        task.document_id or task.knowledge_base_id or task.conversation_id, job.attempts, delay,
                        exc_info=True)
            async with self.sessionmaker() as session, session.begin():
                await session.execute(update(PurgeJob).where(self.queue.fenced(job)).values(
                    status=PurgeStatus.PENDING, lease_expires_at=None,
                    not_before=func.now() + timedelta(seconds=delay), last_error=f"{type(exc).__name__}: {exc}"[:2000]))
            return True
        async with self.sessionmaker() as session, session.begin():
            await session.execute(delete(PurgeJob).where(self.queue.fenced(job)))
        log.info("purged %s %s (%d file(s))", task.kind,
                 task.document_id or task.knowledge_base_id or task.conversation_id, len(task.blob_keys))
        return True

    async def _purge(self, task: PurgeJob) -> None:
        # 每一步都幂等：重试时从头再做一遍也没问题
        if task.kind == PurgeKind.CONVERSATION:
            await self._purge_conversation(task)
            return
        if task.kind == PurgeKind.DOCUMENT:
            await self.index.delete_document(task.document_id)
        else:
            await self.index.delete_knowledge_base(task.knowledge_base_id)
        for key in task.blob_keys:
            await self.blobs.delete(key)
        await self._sync_graph(task.knowledge_base_id)

    async def _purge_conversation(self, task: PurgeJob) -> None:
        for key in task.blob_keys:  # 产物原件（调研报告、交付的图表）
            await self.blobs.delete(key)
        if task.run_ids:
            if self.checkpointer is None:
                raise RuntimeError("checkpointer is not available in this process")
            for run_id in task.run_ids:
                await self.checkpointer.adelete_thread(run_id)  # thread_id 就是 run_id（0006）；幂等
        if self.sandbox is not None:
            await self.sandbox.delete_workspace(str(task.conversation_id))

    async def _sync_graph(self, kb_id: uuid.UUID) -> None:
        """按 PostgreSQL 重写 Neo4j 里这个知识库的图（知识库已删除则整体删掉）。"""
        async with self.sessionmaker() as session:
            kb_exists = await session.scalar(select(exists().where(KnowledgeBase.id == kb_id)))
            if kb_exists and await graph_build_active(session, kb_id):
                return  # 进行中的构建完成时会整体重写 Neo4j，这里不抢着写
            if kb_exists:
                entities = list((await session.scalars(
                    select(GraphEntity).where(GraphEntity.knowledge_base_id == kb_id))).all())
                relations = list((await session.scalars(
                    select(GraphRelation).where(GraphRelation.knowledge_base_id == kb_id))).all())
        if kb_exists:
            await self.graph_store.replace_kb(kb_id, entities, relations)
        else:
            await self.graph_store.delete_kb(kb_id)
