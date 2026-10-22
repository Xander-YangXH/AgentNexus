import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import Document, DocumentChunk, DocumentStatus, KnowledgeBase


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: uuid.UUID, name: str, description: str | None) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self.session.add(kb)
        await self.session.flush()
        return kb

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return await self.session.get(KnowledgeBase, kb_id)

    async def list_by_user(self, user_id: uuid.UUID) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(KnowledgeBase.user_id == user_id).order_by(KnowledgeBase.created_at)
        return list((await self.session.scalars(stmt)).all())


class DocumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, document: Document) -> Document:
        self.session.add(document)
        await self.session.flush()
        return document

    async def get(self, document_id: uuid.UUID) -> Document | None:
        return await self.session.get(Document, document_id)

    async def list_by_kb(self, kb_id: uuid.UUID) -> list[Document]:
        stmt = select(Document).where(Document.knowledge_base_id == kb_id).order_by(Document.created_at.desc())
        return list((await self.session.scalars(stmt)).all())

    async def claim(self, document_id: uuid.UUID) -> bool:
        """pending -> processing 的原子抢占（与 AgentRun.claim 同一模式）：同一文档只会被处理一次。"""
        stmt = (
            update(Document)
            .where(Document.id == document_id, Document.status == DocumentStatus.PENDING)
            .values(status=DocumentStatus.PROCESSING)
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def list_ids_by_status(self, status: DocumentStatus) -> list[uuid.UUID]:
        stmt = select(Document.id).where(Document.status == status).order_by(Document.created_at)
        return list((await self.session.scalars(stmt)).all())

    async def reset(self, from_status: DocumentStatus, *, document_id: uuid.UUID | None = None) -> list[uuid.UUID]:
        """把文档从 from_status 改回 pending（重新处理），返回被改的 id。document_id 为空时改所有符合条件的。"""
        stmt = update(Document).where(Document.status == from_status)
        if document_id is not None:
            stmt = stmt.where(Document.id == document_id)
        stmt = stmt.values(status=DocumentStatus.PENDING, error=None, error_code=None, processed_at=None)
        return list((await self.session.scalars(stmt.returning(Document.id))).all())

    async def finish(
        self, document_id: uuid.UUID, status: DocumentStatus, *, chunk_count: int = 0,
        error: str | None = None, error_code: str | None = None, version: int | None = None,
    ) -> bool:
        """processing -> 终态。version 是租约版本（fencing token，decisions/0020）：给了就必须匹配。"""
        conds = [Document.id == document_id, Document.status == DocumentStatus.PROCESSING]
        if version is not None:
            conds.append(Document.lease_version == version)
        stmt = (
            update(Document)
            .where(*conds)
            .values(status=status, chunk_count=chunk_count, error=error, error_code=error_code, processed_at=func.now())
        )
        return (await self.session.execute(stmt)).rowcount == 1


class ChunkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_all(self, chunks: list[DocumentChunk]) -> None:
        self.session.add_all(chunks)
        await self.session.flush()

    async def list_by_document(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        stmt = select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.seq)
        return list((await self.session.scalars(stmt)).all())
