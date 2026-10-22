"""知识库用例：创建知识库、上传文档；后台处理（解析 → 分块 → 向量化 → 写入 Qdrant → 标记 ready）。

处理由 runtime/ingest.py 的后台派发器调用（沿用“先落库再投递”，decisions/0016）。
"""

import asyncio
import hashlib
import logging
import time
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.chunking import Chunk, chunk_sections
from agentnexus.knowledge.embedding import Embedder
from agentnexus.knowledge.parsing import SUPPORTED_EXTENSIONS, ParseError, extension_of, parse
from agentnexus.knowledge.tokens import EstimatingCounter, TokenCounter
from agentnexus.repositories.knowledge import ChunkRepository, DocumentRepository, KnowledgeBaseRepository
from agentnexus.services.errors import (
    DocumentExists,
    DocumentNotFound,
    DocumentNotRetryable,
    EmptyFile,
    FileTooLarge,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    UnsupportedFileType,
)
from agentnexus.storage.blobs import BlobStore
from agentnexus.storage.postgres import Document, DocumentChunk, DocumentStatus, KnowledgeBase
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
    ):
        self.sessionmaker = sessionmaker
        self.blobs = blobs
        self.embedder = embedder
        self.index = index
        self.max_upload_bytes = max_upload_bytes
        self.token_counter = token_counter or EstimatingCounter()

    # ---------- 知识库 ----------

    async def create_kb(self, user_id: uuid.UUID, name: str, description: str | None) -> KnowledgeBase:
        try:
            async with self.sessionmaker() as session, session.begin():
                return await KnowledgeBaseRepository(session).create(user_id, name, description)
        except IntegrityError as exc:
            raise KnowledgeBaseExists(name) from exc

    async def list_kbs(self, user_id: uuid.UUID) -> list[KnowledgeBase]:
        async with self.sessionmaker() as session:
            return await KnowledgeBaseRepository(session).list_by_user(user_id)

    async def get_kb(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
        async with self.sessionmaker() as session:
            kb = await KnowledgeBaseRepository(session).get(kb_id)
        if kb is None or kb.user_id != user_id:
            raise KnowledgeBaseNotFound(str(kb_id))
        return kb

    # ---------- 文档 ----------

    async def upload(self, user_id: uuid.UUID, kb_id: uuid.UUID, filename: str, data: bytes) -> Document:
        """校验 → 写原件 → 在事务里插入文档（pending）。

        顺序的理由：如果先插记录、再写原件失败，会留下一条“找不到原件”的文档，比多一个没人引用的文件更糟；
        所以先写原件，插入失败时尽力删除它。
        """
        await self.get_kb(user_id, kb_id)  # 归属校验放在写文件之前：不给无权限的请求写任何东西
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

    async def get_document(self, user_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        async with self.sessionmaker() as session:
            doc = await DocumentRepository(session).get(document_id)
            kb = await KnowledgeBaseRepository(session).get(doc.knowledge_base_id) if doc else None
        if doc is None or kb is None or kb.user_id != user_id:
            raise DocumentNotFound(str(document_id))
        return doc

    async def list_chunks(self, user_id: uuid.UUID, document_id: uuid.UUID) -> list[DocumentChunk]:
        await self.get_document(user_id, document_id)
        async with self.sessionmaker() as session:
            return await ChunkRepository(session).list_by_document(document_id)

    async def reprocess(self, user_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """失败的文档重新排队（例如处理时 Qdrant 暂时不可用）。只允许 failed -> pending。"""
        await self.get_document(user_id, document_id)
        async with self.sessionmaker() as session, session.begin():
            if not await DocumentRepository(session).reset(DocumentStatus.FAILED, document_id=document_id):
                raise DocumentNotRetryable(str(document_id))
        return await self.get_document(user_id, document_id)

    # ---------- 处理（后台） ----------

    async def list_pending_ids(self) -> list[uuid.UUID]:
        async with self.sessionmaker() as session:
            return await DocumentRepository(session).list_ids_by_status(DocumentStatus.PENDING)

    async def requeue_interrupted(self) -> list[uuid.UUID]:
        """启动恢复：上一个进程处理到一半的文档改回 pending 重新处理。

        与 run 不同（run 被标为失败，decisions/0009）：文档处理是幂等的、不花钱（本地模型），
        重做前会先清掉索引里的旧点，所以直接重来即可。单进程假设同 RunDispatcher。
        """
        async with self.sessionmaker() as session, session.begin():
            return await DocumentRepository(session).reset(DocumentStatus.PROCESSING)

    async def process(self, document_id: uuid.UUID) -> bool:
        """解析 → 分块 → 向量化 → 写入索引 → 分块落库并标记 ready。返回是否由本次调用处理。

        顺序的理由（PostgreSQL 是事实来源，Qdrant 是派生索引）：
        - 先写索引、再在一个事务里写分块并标记 ready：ready 的文档一定能被检索到；
        - 反过来的失败情形是“索引里多了点、数据库里没有对应分块”，检索时按 id 回数据库取原文会自然过滤掉，
          而且重新处理前会按文档 id 清掉旧点。
        """
        async with self.sessionmaker() as session, session.begin():
            repo = DocumentRepository(session)
            if not await repo.claim(document_id):
                return False
            doc = await repo.get(document_id)
        started = time.perf_counter()
        outcome, chunk_count = await self._process_claimed(doc)
        # 每个文档一条带结局、耗时、分块数的日志（与 run 结束时的日志一致），大文件慢在哪里一眼可见。
        # 文本格式只打印消息本身，所以关键数字也写进消息；JSON 格式另有结构化字段
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info(
            "document %s processed: %s, %d chunk(s), %.1fs", document_id, outcome, chunk_count, duration_ms / 1000,
            extra={"document_id": str(document_id), "outcome": outcome, "chunks": chunk_count, "duration_ms": duration_ms},
        )
        return outcome != "discarded"

    async def _process_claimed(self, doc: Document) -> tuple[str, int]:
        """返回（结局, 分块数）。结局：ready / 失败错误码 / discarded（文档已不在 processing，结果丢弃）。"""
        document_id = doc.id
        try:
            data = await self.blobs.get(doc.blob_key)
            # 解析、分块、向量化都是 CPU 计算，放到线程里，不阻塞事件循环（同进程里还在跑对话和 SSE）
            chunks, vectors = await asyncio.to_thread(self._prepare, doc.filename, data)
        except ParseError as exc:
            await self._fail(document_id, str(exc), exc.code)
            return exc.code, 0
        except Exception as exc:
            log.warning("processing document %s failed", document_id, exc_info=True)
            await self._fail(document_id, f"{type(exc).__name__}: {exc}"[:2000], "internal")
            return "internal", 0

        rows = [
            DocumentChunk(
                id=uuid.uuid4(), document_id=document_id, knowledge_base_id=doc.knowledge_base_id,
                seq=c.seq, text=c.text, token_count=c.token_count, page=c.page,
            )
            for c in chunks
        ]
        try:
            await self.index.delete_document(document_id)  # 上次中断可能留下了点
            await self.index.upsert(
                [ChunkPoint(r.id, doc.knowledge_base_id, document_id, v) for r, v in zip(rows, vectors)]
            )
        except Exception as exc:
            log.warning("indexing document %s failed", document_id, exc_info=True)
            await self._discard_points(document_id)
            await self._fail(document_id, f"{type(exc).__name__}: {exc}"[:2000], "index_unavailable")
            return "index_unavailable", 0

        # 分块与“完成”在同一事务：要么文档 ready 且分块齐全，要么都没有
        async with self.sessionmaker() as session, session.begin():
            await ChunkRepository(session).add_all(rows)
            finished = await DocumentRepository(session).finish(document_id, DocumentStatus.READY, chunk_count=len(rows))
            if not finished:
                await session.rollback()  # 文档已不在 processing（例如被别处改了状态）：分块也不能留下
        if not finished:
            await self._discard_points(document_id)
            return "discarded", 0
        return "ready", len(rows)

    def _prepare(self, filename: str, data: bytes) -> tuple[list[Chunk], list[list[float]]]:
        markdown = extension_of(filename) in (".md", ".markdown")  # 只有 Markdown 的 # 才确定是标题
        chunks = chunk_sections(parse(filename, data), self.token_counter, heading_boundaries=markdown)
        return chunks, self.embedder.embed_documents([c.text for c in chunks])

    async def _discard_points(self, document_id: uuid.UUID) -> None:
        try:
            await self.index.delete_document(document_id)
        except Exception:
            # 清不掉也不影响正确性：检索时没有对应分块的点会被过滤，重新处理前也会再清一次
            log.warning("could not remove index points of document %s", document_id, exc_info=True)
    async def _fail(self, document_id: uuid.UUID, error: str, error_code: str) -> None:
        async with self.sessionmaker() as session, session.begin():
            await DocumentRepository(session).finish(document_id, DocumentStatus.FAILED, error=error, error_code=error_code)
