import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.repositories.users import UserRepository
from agentnexus.services.artifacts import ArtifactService
from agentnexus.services.graph import GraphService
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.storage.postgres import AgentRun
from agentnexus.services.conversations import ConversationService
from agentnexus.services.runs import RunService
from agentnexus.services.users import UserService


def get_sessionmaker(request: Request) -> async_sessionmaker:
    return request.app.state.sessionmaker


def get_dispatcher(request: Request) -> RunDispatcher:
    return request.app.state.dispatcher


def get_event_streamer(request: Request) -> RunEventStreamer:
    return request.app.state.event_streamer


def get_artifact_service(request: Request) -> ArtifactService:
    return request.app.state.artifact_service


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


def get_ingest_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.ingest_dispatcher


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_graph_service(request: Request) -> GraphService:
    return request.app.state.graph_service


def get_graph_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.graph_dispatcher


SessionMaker = Annotated[async_sessionmaker, Depends(get_sessionmaker)]


def get_user_service(sm: SessionMaker) -> UserService:
    return UserService(sm)


def get_conversation_service(sm: SessionMaker) -> ConversationService:
    return ConversationService(sm)


def get_run_service(sm: SessionMaker) -> RunService:
    return RunService(sm)


async def current_user_id(
    sm: SessionMaker,
    x_user_id: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """鉴权占位：阶段 5 前用 X-User-Id 头标识用户，并校验该用户存在。

    之后接入真实认证（token -> user）只需要替换这个依赖，路由与服务层不用动。
    """
    try:
        user_id = uuid.UUID(x_user_id or "")
    except ValueError:
        raise HTTPException(status_code=401, detail="missing or invalid X-User-Id") from None
    async with sm() as session:
        if await UserRepository(session).get(user_id) is None:
            raise HTTPException(status_code=401, detail="unknown user")
    return user_id


CurrentUser = Annotated[uuid.UUID, Depends(current_user_id)]


async def owned_run(
    run_id: uuid.UUID,
    user_id: CurrentUser,
    svc: RunService = Depends(get_run_service),
) -> AgentRun:
    """归属校验做成依赖：依赖在响应开始之前执行，所以流式接口也能正常返回 404，
    而不是先发出 200 响应头、再在流里报错。"""
    return await svc.get(user_id, run_id)


OwnedRun = Annotated[AgentRun, Depends(owned_run)]
