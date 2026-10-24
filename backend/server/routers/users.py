"""登录、当前用户，以及管理员管理用户和部门（decisions/0023）。"""

import dataclasses
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response

from agentnexus.security.auth import AuthService
from agentnexus.security.throttle import LoginThrottle
from server.deps import SESSION_COOKIE, AdminPrincipal, CurrentPrincipal, client_ip, get_auth_service, get_login_throttle
from server.schemas import (
    DepartmentCreate,
    DepartmentOut,
    LoginRequest,
    MeOut,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/api", tags=["users"])


async def _me(principal, auth: AuthService) -> MeOut:
    dept = next((d for d in await auth.list_departments() if d.id == principal.department_id), None)
    return MeOut(id=principal.id, name=principal.name, role=principal.role, department_id=principal.department_id,
                 department_name=dept.name if dept else None)


@router.post("/auth/login", response_model=MeOut)
async def login(body: LoginRequest, request: Request, response: Response,
                auth: AuthService = Depends(get_auth_service), throttle: LoginThrottle = Depends(get_login_throttle)):
    """登录：校验密码，建会话，令牌放进 HttpOnly Cookie（脚本读不到，XSS 偷不走）。

    SameSite=Lax：别的网站发起的跨站 POST 不带这个 Cookie（再加上 CSRF 中间件校验 Origin）。
    Secure 由配置决定：本地 http 演示时不能设，否则浏览器不保存。
    限流（decisions/0025）：同一账号同一 IP、同一 IP 在窗口内尝试过多时返回 429，不再校验密码。
    """
    ip = client_ip(request)
    await throttle.attempt(body.name, ip)
    result = await auth.login(body.name, body.password)
    await throttle.succeeded(body.name, ip)
    max_age = int((result.expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(SESSION_COOKIE, result.token, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.app.state.cookie_secure, path="/")
    return await _me(result.principal, auth)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, auth: AuthService = Depends(get_auth_service)):
    await auth.logout(request.cookies.get(SESSION_COOKIE))  # 删会话：即刻失效，不依赖浏览器删 Cookie
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me", response_model=MeOut)
async def me(principal: CurrentPrincipal, auth: AuthService = Depends(get_auth_service)):
    return await _me(principal, auth)


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(_: CurrentPrincipal, auth: AuthService = Depends(get_auth_service)):
    return await auth.list_departments()


# ---------- 管理员 ----------

@router.post("/admin/departments", response_model=DepartmentOut, status_code=201)
async def create_department(body: DepartmentCreate, _: AdminPrincipal, auth: AuthService = Depends(get_auth_service)):
    return await auth.create_department(body.name)


@router.get("/admin/users", response_model=list[UserOut])
async def list_users(_: AdminPrincipal, auth: AuthService = Depends(get_auth_service)):
    return await auth.list_users()


@router.post("/admin/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, _: AdminPrincipal, auth: AuthService = Depends(get_auth_service)):
    return await auth.create_user(body.name, body.password, role=body.role, department_id=body.department_id)


@router.patch("/admin/users/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, body: UserUpdate, admin: AdminPrincipal,
                      auth: AuthService = Depends(get_auth_service)):
    """改角色、部门、禁用、重置密码。禁用或重置密码会让该用户的所有会话立即失效。"""
    fields = body.model_dump(exclude_unset=True)
    return await auth.update_user(
        user_id, role=fields.get("role"), disabled=fields.get("disabled"), password=fields.get("password"),
        department_id=fields.get("department_id", dataclasses.MISSING),
    )
