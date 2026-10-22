import uuid

from fastapi import APIRouter, Depends, UploadFile

from agentnexus.services.errors import FileTooLarge
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.runtime.jobs import JobDispatcher
from server.deps import CurrentUser, get_ingest_dispatcher, get_knowledge_service, get_search_service
from server.schemas import ChunkOut, DocumentOut, KnowledgeBaseCreate, KnowledgeBaseOut, SearchOut, SearchRequest

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
    kb_id: uuid.UUID,
    file: UploadFile,
    user_id: CurrentUser,
    svc: KnowledgeService = Depends(get_knowledge_service),
    ingest: JobDispatcher = Depends(get_ingest_dispatcher),
):
    """上传文档：先落库（pending）再投递给后台处理（解析、分块、向量化），用文档 id 查进度。
    投递丢了也没关系，后台扫描会补上（数据库为准）。"""
    # 最多只读“上限 + 1”字节：超出就拒绝，不把一个超大文件完整读进内存
    data = await file.read(svc.max_upload_bytes + 1)
    if len(data) > svc.max_upload_bytes:
        raise FileTooLarge(file.filename or "")
    doc = await svc.upload(user_id, kb_id, file.filename or "", data)
    ingest.enqueue(doc.id)
    return doc


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(kb_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.list_documents(user_id, kb_id)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    return await svc.get_document(user_id, document_id)


@router.post("/documents/{document_id}/reprocess", response_model=DocumentOut, status_code=202)
async def reprocess_document(
    document_id: uuid.UUID,
    user_id: CurrentUser,
    svc: KnowledgeService = Depends(get_knowledge_service),
    ingest: JobDispatcher = Depends(get_ingest_dispatcher),
):
    """重新处理失败的文档（例如处理时向量库暂时不可用）。只允许 failed 的文档。"""
    doc = await svc.reprocess(user_id, document_id)
    ingest.enqueue(doc.id)
    return doc


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(document_id: uuid.UUID, user_id: CurrentUser, svc: KnowledgeService = Depends(get_knowledge_service)):
    """分块原文：用于检查分块效果，以及以后展示引用出处。"""
    return await svc.list_chunks(user_id, document_id)


@router.post("/knowledge/search", response_model=SearchOut)
async def search(body: SearchRequest, user_id: CurrentUser, svc: SearchService = Depends(get_search_service)):
    """在一个或多个自己的知识库里检索（检索测试页面用；2.4 的 knowledge_search 工具走同一个服务）。"""
    return await svc.search(user_id, body.knowledge_base_ids, body.query, top_k=body.top_k, rerank=body.rerank)
