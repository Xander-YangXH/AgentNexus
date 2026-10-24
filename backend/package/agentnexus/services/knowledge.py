"""知识库用例：创建知识库、上传文档；后台处理（解析 → 分块 → 向量化 → 写入 Qdrant → 标记 ready）。

处理由 runtime/ingest.py 的后台派发器调用（沿用“先落库再投递”，decisions/0016）。
"""

import asyncio
import hashlib
import logging
import time
import uuid
from pathlib import PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.chunking import Chunk, chunk_sections
from agentnexus.knowledge.embedding import Embedder
from agentnexus.knowledge.isolation import parse_isolated
from agentnexus.knowledge.parsing import SUPPORTED_EXTENSIONS, ParseError, extension_of, parse
from agentnexus.knowledge.tokens import EstimatingCounter, TokenCounter
from agentnexus.runtime.lease import Claimed, LeaseQueue, notify_jobs
from agentnexus.repositories.knowledge import ChunkRepository, DocumentRepository, KnowledgeBaseRepository
from agentnexus.services.errors import (
    DocumentExists,
    DocumentNotFound,
    DocumentNotRetryable,
    EmptyFile,
    FileTooLarge,
    GraphBuildInProgress,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    NoDepartment,
    UnsupportedFileType,
)
from agentnexus.security import access
from agentnexus.services.purge import graph_build_active, prune_graph
from agentnexus.storage.blobs import BlobStore
from agentnexus.storage.postgres import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
    PurgeJob,
    PurgeKind,
    Visibility,
)
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint

log = logging.getLogger(__name__)


def clean_filename(name: str) -> str:
    """只保留文件名本身（去掉客户端传来的路径，Windows 的反斜杠也算），截断到 255 个字符。"""
    base = PurePosixPath(name.replace("\\", "/")).name.strip()
    return (base or "unnamed")[:255]


class KnowledgeService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        blobs: BlobStore,
        embedder: Embedder,
        index: ChunkIndex,
        *,
        max_upload_bytes: int = 10 * 1024 * 1024,
        token_counter: TokenCounter | None = None,
        parse_timeout: float = 60.0,
        parse_memory_mb: int = 1024,
        lease_seconds: float = 30.0,
    ):
        self.sessionmaker = sessionmaker
        self.queue = LeaseQueue(
            sessionmaker, Document, pending=DocumentStatus.PENDING, running=DocumentStatus.PROCESSING,
            lease_seconds=lease_seconds, notify_kind="ingest",
        )
        self.blobs = blobs
        self.embedder = embedder
        self.index = index
        self.max_upload_bytes = max_upload_bytes
        self.token_counter = token_counter or EstimatingCounter()
        self.parse_timeout = parse_timeout
        self.parse_memory_mb = parse_memory_mb
        self.parser = parse  # 测试可以换成“恶意”的解析函数（必须是模块级函数，子进程按名字导入）

    # ---------- 知识库 ----------

    async def create_kb(self, user_id: uuid.UUID, name: str, description: str | None) -> KnowledgeBase:
        try:
            async with self.sessionmaker() as session, session.begin():
                return await KnowledgeBaseRepository(session).create(user_id, name, description)
        except IntegrityError as exc:
            raise KnowledgeBaseExists(name) from exc

    async def list_kbs(self, user_id: uuid.UUID) -> list[KnowledgeBase]:
        """自己的，加上同部门共享的（decisions/0023）。"""
        async with self.sessionmaker() as session:
            dept = await access.department_of(session, user_id)
            stmt = select(KnowledgeBase).where(access.visible_clause(user_id, dept)).order_by(KnowledgeBase.created_at)
            return list((await session.scalars(stmt)).all())

    async def get_kb(self, user_id: uuid.UUID, kb_id: uuid.UUID, *, write: bool = False) -> KnowledgeBase:
        """读：自己的或同部门共享的；write=True：只有所有者（共享的返回 403 只读）。"""
        async with self.sessionmaker() as session:
            return await (access.writable_kb if write else access.readable_kb)(session, user_id, kb_id)

    async def set_visibility(self, user_id: uuid.UUID, kb_id: uuid.UUID, visibility: str) -> KnowledgeBase:
        """改可见性。共享到“所有者当前所在的部门”；没有部门的用户不能共享。"""
        async with self.sessionmaker() as session, session.begin():
            kb = await access.writable_kb(session, user_id, kb_id)
            if visibility == Visibility.DEPARTMENT:
                dept = await access.department_of(session, user_id)
                if dept is None:
                    raise NoDepartment(str(user_id))
                kb.department_id = dept
            else:
                kb.department_id = None
            kb.visibility = visibility
            await session.flush()
            return kb

    async def delete_kb(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """删除知识库（只有所有者）。PostgreSQL 里的数据在这个事务里删掉，派生副本由清理任务删（decisions/0026）。

        级联删掉：分块、图谱（实体、关系、出处、抽取缓存、构建记录）、对话与它的关联（包括同部门成员的对话）。
        对话里已有回答的引用是快照，保留：那是用户当时看到的回答的一部分。
        """
        async with self.sessionmaker() as session, session.begin():
            await self._lock_writable_kb(session, user_id, kb_id)
            blob_keys = list((await session.scalars(
                delete(Document).where(Document.knowledge_base_id == kb_id).returning(Document.blob_key))).all())
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            session.add(PurgeJob(kind=PurgeKind.KNOWLEDGE_BASE, knowledge_base_id=kb_id, blob_keys=blob_keys))
            await notify_jobs(session, "purge")

    async def _lock_writable_kb(self, session, user_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """删除前：校验是所有者，锁住知识库这一行，并确认没有进行中的图谱构建。

        行锁让“删除”和“新建图谱构建”串行：构建记录对知识库有外键，插入时要拿这一行的共享锁，
        会等删除事务结束；反过来，已经提交的构建会在这里被看到。构建进行中不允许删除：它会用删除前读到的分块
        写出出处，并在结束时重写 Neo4j。
        """
        await access.writable_kb(session, user_id, kb_id)
        await session.execute(select(KnowledgeBase.id).where(KnowledgeBase.id == kb_id).with_for_update())
        if await graph_build_active(session, kb_id):
            raise GraphBuildInProgress(str(kb_id))

    # ---------- 文档 ----------

    async def upload(self, user_id: uuid.UUID, kb_id: uuid.UUID, filename: str, data: bytes) -> Document:
        """校验 → 写原件 → 在事务里插入文档（pending）。

        顺序的理由：如果先插记录、再写原件失败，会留下一条“找不到原件”的文档，比多一个没人引用的文件更糟；
        所以先写原件，插入失败时尽力删除它。
        """
        await self.get_kb(user_id, kb_id, write=True)  # 归属校验放在写文件之前：不给无权限的请求写任何东西
        filename = clean_filename(filename)
        if extension_of(filename) not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFileType(filename)
        if not data:
            raise EmptyFile(filename)
        if len(data) > self.max_upload_bytes:
            raise FileTooLarge(filename)

        sha256 = hashlib.sha256(data).hexdigest()
        document_id = uuid.uuid4()
        blob_key = f"documents/{document_id}"  # 不用文件名做 key：避免奇怪字符和重名
        await self.blobs.put(blob_key, data)
        try:
            async with self.sessionmaker() as session, session.begin():
                await notify_jobs(session, "ingest")  # 提交时才送达（decisions/0020）
                return await DocumentRepository(session).add(
                    Document(
                        id=document_id, knowledge_base_id=kb_id, filename=filename, size_bytes=len(data),
                        sha256=sha256, blob_key=blob_key, status=DocumentStatus.PENDING, chunk_count=0,
                    )
                )
        except IntegrityError as exc:
            await self.blobs.delete(blob_key)
            if "uq_documents_kb_sha256" in str(exc.orig):
                raise DocumentExists(await self._existing_id(kb_id, sha256)) from exc
            raise
        except BaseException:
            await self.blobs.delete(blob_key)
            raise

    async def _existing_id(self, kb_id: uuid.UUID, sha256: str) -> uuid.UUID | None:
        async with self.sessionmaker() as session:
            stmt = select(Document.id).where(Document.knowledge_base_id == kb_id, Document.sha256 == sha256)
            return (await session.scalars(stmt)).first()

    async def list_documents(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> list[Document]:
        await self.get_kb(user_id, kb_id)
        async with self.sessionmaker() as session:
            return await DocumentRepository(session).list_by_kb(kb_id)

    async def get_document(self, user_id: uuid.UUID, document_id: uuid.UUID, *, write: bool = False) -> Document:
        async with self.sessionmaker() as session:
            doc = await DocumentRepository(session).get(document_id)
            if doc is None:
                raise DocumentNotFound(str(document_id))
            try:
                await (access.writable_kb if write else access.readable_kb)(session, user_id, doc.knowledge_base_id)
            except KnowledgeBaseNotFound:
                raise DocumentNotFound(str(document_id)) from None
        return doc

    async def list_chunks(self, user_id: uuid.UUID, document_id: uuid.UUID) -> list[DocumentChunk]:
        await self.get_document(user_id, document_id)
        async with self.sessionmaker() as session:
            return await ChunkRepository(session).list_by_document(document_id)

    async def delete_document(self, user_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """删除文档（只有知识库所有者）。处理中的也可以删：入库的写回都带租约版本，行没了就写不进去，
        worker 会丢弃自己写的点（decisions/0020）；它的心跳也会发现租约没了而停止。"""
        doc = await self.get_document(user_id, document_id, write=True)
        async with self.sessionmaker() as session, session.begin():
            await self._lock_writable_kb(session, user_id, doc.knowledge_base_id)
            blob_key = await session.scalar(
                delete(Document).where(Document.id == document_id).returning(Document.blob_key))
            if blob_key is None:
                raise DocumentNotFound(str(document_id))  # 同时有另一个删除请求先完成了
            await prune_graph(session, doc.knowledge_base_id)
            session.add(PurgeJob(kind=PurgeKind.DOCUMENT, knowledge_base_id=doc.knowledge_base_id,
                                 document_id=document_id, blob_keys=[blob_key]))
            await notify_jobs(session, "purge")

    async def reprocess(self, user_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """失败的文档重新排队（例如处理时 Qdrant 暂时不可用）。只允许 failed -> pending。"""
        await self.get_document(user_id, document_id, write=True)
        async with self.sessionmaker() as session, session.begin():
            if not await DocumentRepository(session).reset(DocumentStatus.FAILED, document_id=document_id):
                raise DocumentNotRetryable(str(document_id))
            await notify_jobs(session, "ingest")
        return await self.get_document(user_id, document_id)

    # ---------- 处理（后台，租约见 decisions/0020） ----------

    async def process(self, document_id: uuid.UUID) -> bool:
        """认领并立即处理指定文档（同步调用：测试、命令行）。后台 worker 走 queue.claim_next + execute。"""
        job = await self.queue.claim(document_id, "inline")
        return await self.execute(job) if job else False

    async def execute(self, job: Claimed) -> bool:
        """解析 → 分块 → 向量化 → 写入索引 → 分块落库并标记 ready。返回结果是否被采用（租约丢了则不采用）。

        顺序的理由（PostgreSQL 是事实来源，Qdrant 是派生索引）：
        - 先写索引、再在一个事务里写分块并标记 ready：ready 的文档一定能被检索到；
        - 反过来的失败情形是“索引里多了点、数据库里没有对应分块”，检索时按 id 回数据库取原文会自然过滤掉。
        并发安全（多个 worker）：
        - 所有数据库写回都带租约版本（fencing），被接管后旧 worker 写不进去；
        - Qdrant 不认版本号，所以不“按文档删除”：失败时只删自己写的点；成功提交的执行者才清理这个文档里不属于本次结果的点。
        """
        async with self.sessionmaker() as session:
            doc = await DocumentRepository(session).get(job.id)
        if doc is None:  # 认领之后、开始处理之前被删除了（decisions/0026）
            log.info("document %s was deleted before processing started", job.id)
            return False
        started = time.perf_counter()
        outcome, chunk_count = await self._execute(doc, job)
        # 每个文档一条带结局、耗时、分块数的日志（与 run 结束时的日志一致），大文件慢在哪里一眼可见。
        # 文本格式只打印消息本身，所以关键数字也写进消息；JSON 格式另有结构化字段
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info(
            "document %s processed: %s, %d chunk(s), %.1fs", job.id, outcome, chunk_count, duration_ms / 1000,
            extra={"document_id": str(job.id), "outcome": outcome, "chunks": chunk_count, "duration_ms": duration_ms,
                   "attempt": job.attempts},
        )
        return outcome != "discarded"

    async def _execute(self, doc: Document, job: Claimed) -> tuple[str, int]:
        """返回（结局, 分块数）。结局：ready / 失败错误码 / discarded（租约已不是自己的，结果丢弃）。"""
        document_id = doc.id
        try:
            data = await self.blobs.get(doc.blob_key)
            # 解析、分块、向量化都是 CPU 计算，放到线程里，不阻塞事件循环
            chunks, vectors = await asyncio.to_thread(self._prepare, doc.filename, data)
        except ParseError as exc:
            await self._fail(job, str(exc), exc.code)
            return exc.code, 0
        except Exception as exc:
            log.warning("processing document %s failed", document_id, exc_info=True)
            await self._fail(job, f"{type(exc).__name__}: {exc}"[:2000], "internal")
            return "internal", 0

        rows = [
            DocumentChunk(
                id=uuid.uuid4(), document_id=document_id, knowledge_base_id=doc.knowledge_base_id,
                seq=c.seq, text=c.text, token_count=c.token_count, page=c.page,
            )
            for c in chunks
        ]
        own_points = [r.id for r in rows]
        try:
            await self.index.upsert(
                [ChunkPoint(r.id, doc.knowledge_base_id, document_id, v, r.text) for r, v in zip(rows, vectors, strict=True)]
            )
        except Exception as exc:
            log.warning("indexing document %s failed", document_id, exc_info=True)
            await self._discard_points(own_points)
            await self._fail(job, f"{type(exc).__name__}: {exc}"[:2000], "index_unavailable")
            return "index_unavailable", 0

        # 分块与“完成”在同一事务：要么文档 ready 且分块齐全，要么都没有。
        # 先做带版本号的状态更新（fencing），通过了才写分块：第一版先写分块，被接管的旧 worker 会撞上接管者分块的
        # 唯一约束直接抛异常，走不到“丢弃自己写的点”那一步（测试发现）
        async with self.sessionmaker() as session, session.begin():
            finished = await DocumentRepository(session).finish(
                document_id, DocumentStatus.READY, chunk_count=len(rows), version=job.version
            )
            if finished:
                await ChunkRepository(session).add_all(rows)
        if not finished:
            await self._discard_points(own_points)
            return "discarded", 0
        # 赢家清场：删掉这个文档里不属于本次结果的点（之前中断的尝试、被接管的旧 worker 写的）
        try:
            await self.index.delete_document_except(document_id, own_points)
        except Exception:
            log.warning("could not clean up stale points of document %s", document_id, exc_info=True)
        return "ready", len(rows)

    def _prepare(self, filename: str, data: bytes) -> tuple[list[Chunk], list[list[float]]]:
        markdown = extension_of(filename) in (".md", ".markdown")  # 只有 Markdown 的 # 才确定是标题
        # PDF / Word 在子进程里解析，有超时和内存上限：一个构造的恶意文件不能卡死整条入库流水线（decisions/0025）
        sections = parse_isolated(filename, data, timeout=self.parse_timeout, mem_mb=self.parse_memory_mb,
                                  parser=self.parser)
        chunks = chunk_sections(sections, self.token_counter, heading_boundaries=markdown)
        return chunks, self.embedder.embed_documents([c.text for c in chunks])

    async def _discard_points(self, ids: list[uuid.UUID]) -> None:
        try:
            await self.index.delete_points(ids)
        except Exception:
            # 清不掉也不影响正确性：检索时没有对应分块的点会被过滤，下次成功处理时也会清掉
            log.warning("could not remove index points %d", len(ids), exc_info=True)

    async def _fail(self, job: Claimed, error: str, error_code: str) -> None:
        async with self.sessionmaker() as session, session.begin():
            await DocumentRepository(session).finish(
                job.id, DocumentStatus.FAILED, error=error, error_code=error_code, version=job.version
            )
