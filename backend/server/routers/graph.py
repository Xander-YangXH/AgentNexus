"""知识图谱接口（decisions/0019）：预估 → 构建（后台）→ 浏览与溯源。"""

import uuid

from fastapi import APIRouter, Depends, Query

from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.services.graph import GraphService
from server.deps import CurrentUser, get_graph_dispatcher, get_graph_service
from server.schemas import EntityDetailOut, GraphBuildOut, GraphEstimateOut, GraphNode, GraphOut

router = APIRouter(prefix="/api", tags=["graph"])


@router.get("/knowledge-bases/{kb_id}/graph/estimate", response_model=GraphEstimateOut)
async def estimate(kb_id: uuid.UUID, user_id: CurrentUser, svc: GraphService = Depends(get_graph_service)):
    """构建前的预估：多少分块、去重后多少、多少命中缓存、需要调用模型多少次、大约多少钱。"""
    return await svc.estimate(user_id, kb_id)


@router.post("/knowledge-bases/{kb_id}/graph/builds", response_model=GraphBuildOut, status_code=202)
async def create_build(
    kb_id: uuid.UUID,
    user_id: CurrentUser,
    svc: GraphService = Depends(get_graph_service),
    jobs: JobDispatcher = Depends(get_graph_dispatcher),
):
    """开始构建（会调用模型、产生费用）：先落库（pending）再投递给后台；用构建 id 查进度。"""
    build = await svc.create_build(user_id, kb_id)
    jobs.enqueue(build.id)
    return build


@router.get("/knowledge-bases/{kb_id}/graph/builds", response_model=list[GraphBuildOut])
async def list_builds(kb_id: uuid.UUID, user_id: CurrentUser, svc: GraphService = Depends(get_graph_service)):
    return await svc.list_builds(user_id, kb_id)


@router.get("/knowledge-bases/{kb_id}/graph/overview", response_model=GraphOut)
async def overview(
    kb_id: uuid.UUID, user_id: CurrentUser, limit: int = Query(60, ge=1, le=300),
    svc: GraphService = Depends(get_graph_service),
):
    """初始视图：被提及最多的实体及它们之间的关系。"""
    return await svc.overview(user_id, kb_id, limit)


@router.get("/knowledge-bases/{kb_id}/graph/search", response_model=list[GraphNode])
async def search(
    kb_id: uuid.UUID, user_id: CurrentUser, q: str = Query(min_length=1, max_length=100),
    svc: GraphService = Depends(get_graph_service),
):
    return await svc.search(user_id, kb_id, q)


@router.get("/knowledge-bases/{kb_id}/graph/path", response_model=GraphOut | None)
async def path(
    kb_id: uuid.UUID, user_id: CurrentUser, source: uuid.UUID, target: uuid.UUID,
    svc: GraphService = Depends(get_graph_service),
):
    """两个实体之间的最短关联（最多 4 跳，不看方向）；不连通时返回 null。"""
    return await svc.path(user_id, kb_id, source, target)


@router.get("/graph/entities/{entity_id}", response_model=EntityDetailOut)
async def entity_detail(entity_id: uuid.UUID, user_id: CurrentUser, svc: GraphService = Depends(get_graph_service)):
    """实体详情与出处原文（来自 PostgreSQL）。"""
    return await svc.entity_detail(user_id, entity_id)


@router.get("/graph/entities/{entity_id}/neighbors", response_model=GraphOut)
async def neighbors(
    entity_id: uuid.UUID, user_id: CurrentUser, limit: int = Query(40, ge=1, le=200),
    svc: GraphService = Depends(get_graph_service),
):
    """一跳展开：按邻居的提及次数取前 limit 个。"""
    return await svc.neighbors(user_id, entity_id, limit)
