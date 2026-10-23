import uuid
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.security.auth import AuthService
from agentnexus.security.principal import Principal, principal_var
from agentnexus.services.artifacts import ArtifactService
from agentnexus.services.graph import GraphService
from agentnexus.services.usage import UsageService
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


def get_usage_service(request: Request) -> UsageService:
    return request.app.state.usage_service


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


SESSION_COOKIE = "agentnexus_session"


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


async def current_principal(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Principal:
    """当前登录用户（decisions/0023）：Cookie 里的会话令牌 → 会话表 → 用户。

    解析出来后放进上下文变量：数据库会话在每个事务开始时据此设置 RLS 身份（security/principal.py）。
    必须是 async 依赖：同步依赖在线程池里执行，上下文变量的修改传不回请求的上下文。
    阶段 5 之前这里是 X-User-Id 占位；路由和服务层都没有改动。
    """
    principal = await auth.resolve(session_token)
    if principal is None:
        raise HTTPException(status_code=401, detail="not logged in")
    principal_var.set(principal)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def current_user_id(principal: CurrentPrincipal) -> uuid.UUID:
    return principal.id


CurrentUser = Annotated[uuid.UUID, Depends(current_user_id)]


def require_role(*roles: str):
    async def check(principal: CurrentPrincipal) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status_code=403, detail="permission denied")
        return principal

    return check


AdminPrincipal = Annotated[Principal, Depends(require_role("admin"))]


async def owned_run(
    run_id: uuid.UUID,
    user_id: CurrentUser,
    svc: RunService = Depends(get_run_service),
) -> AgentRun:
    """归属校验做成依赖：依赖在响应开始之前执行，所以流式接口也能正常返回 404，
    而不是先发出 200 响应头、再在流里报错。"""
    return await svc.get(user_id, run_id)


OwnedRun = Annotated[AgentRun, Depends(owned_run)]
