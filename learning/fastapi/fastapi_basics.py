"""
FastAPI 基础示例 —— 路由、路径/查询参数、请求体校验（Pydantic）、response_model、
错误处理、APIRouter 拆分路由、lifespan 生命周期、CORS、async 阻塞陷阱。
用"工单"当业务场景，对应 AgentNexus 的智能客服与工单自动化。

运行方式：
    uvicorn fastapi_basics:app --reload --port 8000
然后打开 http://127.0.0.1:8000/docs 交互式测试，详细步骤见 FASTAPI_GUIDE.md。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Path, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ---------- lifespan：应用启动/关闭时执行的逻辑 ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # yield 之前 = 启动阶段：真实项目里在这里建数据库连接池、Redis 客户端、加载配置
    print("[lifespan] 应用启动")
    yield
    # yield 之后 = 关闭阶段：释放连接、把缓冲数据落盘
    print("[lifespan] 应用关闭")


app = FastAPI(title="AgentNexus FastAPI 基础示例", version="0.1.0", lifespan=lifespan)

# CORS：前端 Vue 开发服务器（localhost:5173）和后端不同源，浏览器默认会拦截跨域请求，
# 需要后端显式声明允许哪些来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Pydantic 模型：请求体校验 + 响应结构 ----------
class TicketCreate(BaseModel):
    """创建工单时客户端提交的数据。校验规则写在类型注解里，不合法会自动返回 422。"""

    title: str = Field(min_length=3, max_length=100, description="工单标题")
    priority: int = Field(default=3, ge=1, le=5, description="优先级 1(最高)~5(最低)")
    description: Optional[str] = None


class Ticket(TicketCreate):
    """服务端返回的工单：在创建数据基础上多了 id 和状态。"""

    id: int
    status: str = "open"


# 用内存字典模拟数据库（重启就没了，仅用于学习）
_db: dict[int, Ticket] = {}
_next_id = 1

# ---------- APIRouter：把一组相关路由拆到单独对象里 ----------
# AgentNexus 里 backend/server/routers/ 下每个文件就是一个 APIRouter，
# 最后在入口处统一 include_router。
router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=Ticket, status_code=status.HTTP_201_CREATED)
async def create_ticket(payload: TicketCreate):
    """请求体（JSON）会被自动解析并按 TicketCreate 校验；response_model 决定返回结构。"""
    global _next_id
    ticket = Ticket(id=_next_id, **payload.model_dump())
    _db[_next_id] = ticket
    _next_id += 1
    return ticket


@router.get("", response_model=list[Ticket])
async def list_tickets(
    status_filter: Optional[str] = Query(default=None, alias="status", description="按状态过滤"),
    limit: int = Query(default=10, ge=1, le=100, description="最多返回多少条"),
):
    """查询参数：不在路径里、写在 URL 问号后面的参数，如 /tickets?status=open&limit=5。"""
    items = list(_db.values())
    if status_filter:
        items = [t for t in items if t.status == status_filter]
    return items[:limit]


@router.get("/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: int = Path(ge=1, description="工单 ID")):
    """路径参数：URL 路径的一部分，如 /tickets/3。找不到时用 HTTPException 返回 404。"""
    ticket = _db.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"工单 {ticket_id} 不存在")
    return ticket


@router.patch("/{ticket_id}/close", response_model=Ticket)
async def close_ticket(ticket_id: int = Path(ge=1)):
    ticket = _db.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"工单 {ticket_id} 不存在")
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="工单已经关闭，不能重复关闭")
    ticket.status = "closed"
    return ticket


app.include_router(router)


# ---------- async 阻塞陷阱：本文件最值得亲手做实验的部分 ----------
# 事件循环是单线程的：async def 里如果调用了同步阻塞函数（time.sleep、requests.get、
# 同步数据库驱动），整个事件循环被卡住，期间所有其他请求都得排队。
@app.get("/blocking/bad", tags=["async 陷阱"])
async def blocking_bad():
    """错误示范：async def 里用 time.sleep，阻塞整个事件循环。"""
    time.sleep(1)
    return {"result": "bad: 阻塞了事件循环"}


@app.get("/blocking/good", tags=["async 陷阱"])
async def blocking_good():
    """正确写法：async def 里用 await asyncio.sleep，让出控制权给其他请求。"""
    await asyncio.sleep(1)
    return {"result": "good: 没有阻塞事件循环"}


@app.get("/blocking/sync-ok", tags=["async 陷阱"])
def blocking_sync_ok():
    """另一种可行写法：普通 def。FastAPI 会把它丢进线程池执行，阻塞调用不会卡住事件循环。"""
    time.sleep(1)
    return {"result": "ok: 普通 def 被放进线程池执行"}


@app.get("/health")
async def health():
    return {"status": "ok"}
