import uuid

from fastapi import APIRouter, Depends, UploadFile

from agentnexus.services.errors import FileTooLarge
from agentnexus.services.knowledge import KnowledgeService
from server.deps import CurrentUser, get_knowledge_service
from server.schemas import ChunkOut, DocumentOut, KnowledgeBaseCreate, KnowledgeBaseOut

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreate, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)
):
    return await svc.create_kb(user_id, body.name, body.description)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.list_kbs(user_id)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(kb_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.get_kb(user_id, kb_id)


@router.post("/knowledge-bases/{kb_id}/documents", response_model=DocumentOut, status_code=202)
async def upload_document(
    kb_id: uuid.UUID, file: UploadFile, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)
):
    """上传文档：先落库（pending），解析、分块、入库是异步的（2.2 起由后台任务处理），用文档 id 查进度。"""
    # 最多只读“上限 + 1”字节：超出就拒绝，不把一个超大文件完整读进内存
    data = await file.read(svc.max_upload_bytes + 1)
    if len(data) > svc.max_upload_bytes:
        raise FileTooLarge(file.filename or "")
    return await svc.upload(user_id, kb_id, file.filename or "", data)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(kb_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.list_documents(user_id, kb_id)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.get_document(user_id, document_id)


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(document_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    """分块原文：用于检查分块效果，以及以后展示引用出处。"""
    return await svc.list_chunks(user_id, document_id)
