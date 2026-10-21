"""知识库用例（阶段 2.1）：创建知识库、上传文档、解析与分块。

2.2 会在 process() 里加上“计算向量、写入 Qdrant”，并由后台派发器调用（沿用“先落库再投递”）。
"""

import hashlib
import logging
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.knowledge.chunking import chunk_sections
from agentnexus.knowledge.parsing import SUPPORTED_EXTENSIONS, ParseError, extension_of, parse
from agentnexus.knowledge.tokens import EstimatingCounter, TokenCounter
from agentnexus.repositories.knowledge import ChunkRepository, DocumentRepository, KnowledgeBaseRepository
from agentnexus.services.errors import (
    DocumentExists,
    DocumentNotFound,
    EmptyFile,
    FileTooLarge,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    UnsupportedFileType,
)
from agentnexus.storage.blobs import BlobStore
from agentnexus.storage.postgres import Document, DocumentChunk, DocumentStatus, KnowledgeBase

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
        *,
        max_upload_bytes: int = 10 * 1024 * 1024,
        token_counter: TokenCounter | None = None,
    ):
        self.sessionmaker = sessionmaker
        self.blobs = blobs
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

    # ---------- 处理 ----------

    async def process(self, document_id: uuid.UUID) -> bool:
        """解析并分块。返回是否由本次调用处理（被别人抢走或已处理过返回 False）。"""
        async with self.sessionmaker() as session, session.begin():
            repo = DocumentRepository(session)
            if not await repo.claim(document_id):
                return False
            doc = await repo.get(document_id)
        try:
            data = await self.blobs.get(doc.blob_key)
            sections = parse(doc.filename, data)
            chunks = chunk_sections(sections, self.token_counter)
        except ParseError as exc:
            await self._fail(document_id, str(exc), exc.code)
            return True
        except Exception as exc:
            log.warning("processing document %s failed", document_id, exc_info=True)
            await self._fail(document_id, f"{type(exc).__name__}: {exc}"[:2000], "internal")
            return True

        # 分块与“完成”在同一事务：要么文档 ready 且分块齐全，要么都没有
        async with self.sessionmaker() as session, session.begin():
            await ChunkRepository(session).add_all(
                [
                    DocumentChunk(
                        document_id=document_id, knowledge_base_id=doc.knowledge_base_id,
                        seq=c.seq, text=c.text, token_count=c.token_count, page=c.page,
                    )
                    for c in chunks
                ]
            )
            if not await DocumentRepository(session).finish(document_id, DocumentStatus.READY, chunk_count=len(chunks)):
                await session.rollback()  # 文档已不在 processing（例如被别处改了状态）：分块也不能留下
                return False
        return True

    async def _fail(self, document_id: uuid.UUID, error: str, error_code: str) -> None:
        async with self.sessionmaker() as session, session.begin():
            await DocumentRepository(session).finish(document_id, DocumentStatus.FAILED, error=error, error_code=error_code)
