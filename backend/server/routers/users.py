from fastapi import APIRouter, Depends

from server.deps import get_user_service
from server.schemas import UserCreate, UserOut
from agentnexus.services.users import UserService

router = APIRouter(prefix="/api/users", tags=["users"])


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, svc: UserService = Depends(get_user_service)):
    return await svc.create(body.name)
