from fastapi import APIRouter, Depends

from server.deps import get_user_service
from server.schemas import UserCreate, UserOut
from agentnexus.services.users import UserService

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(svc: UserService = Depends(get_user_service)):
    """列出所有用户，供前端的“选择用户”页使用。

    仅是鉴权占位期的开发便利：它不需要登录、会暴露所有用户名。阶段 5 接入真实认证后删除。
    """
    return await svc.list()


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, svc: UserService = Depends(get_user_service)):
    return await svc.create(body.name)
