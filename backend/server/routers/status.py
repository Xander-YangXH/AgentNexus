"""运行状态（decisions/0031）：管理员看 JSON；Prometheus 抓 /metrics。

/metrics 不在 /api 下：Nginx 只转发 /api/，所以它不会暴露到外网，只能在内部网络（或本机的 api 端口）访问，
因此不要求登录（Prometheus 抓取不带会话）。返回的只有计数和时长。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from agentnexus.services.status import prometheus, system_status
from server.deps import AdminPrincipal, get_sessionmaker

router = APIRouter(tags=["status"])


@router.get("/api/admin/status")
async def admin_status(_: AdminPrincipal, sessionmaker=Depends(get_sessionmaker)):
    return await system_status(sessionmaker)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(sessionmaker=Depends(get_sessionmaker)):
    return PlainTextResponse(prometheus(await system_status(sessionmaker)), media_type="text/plain; version=0.0.4")
