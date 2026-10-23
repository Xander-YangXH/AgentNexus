"""用量看板（decisions/0023）。"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from agentnexus.services.usage import UsageService
from server.deps import CurrentPrincipal, get_usage_service

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
async def usage_summary(
    principal: CurrentPrincipal,
    scope: Literal["me", "department", "all"] = "me",
    days: int = Query(default=30, ge=1, le=90),
    department_id: uuid.UUID | None = None,
    svc: UsageService = Depends(get_usage_service),
):
    """按天的请求数、模型调用次数、token、检索次数和估算费用。

    范围：me（所有人）；department（部门负责人看本部门，管理员可指定部门）；all（管理员）。越权返回 403。
    """
    try:
        return await svc.summary(principal, days=days, scope=scope, department_id=department_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
