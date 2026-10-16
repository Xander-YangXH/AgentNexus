import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.repositories.users import UserRepository
from agentnexus.services.conversations import ConversationService
from agentnexus.services.runs import RunService
from agentnexus.services.users import UserService


def get_sessionmaker(request: Request) -> async_sessionmaker:
    return request.app.state.sessionmaker


def get_dispatcher(request: Request) -> RunDispatcher:
    return request.app.state.dispatcher


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
