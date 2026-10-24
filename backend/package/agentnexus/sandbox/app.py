"""sandbox 服务（decisions/0021）：唯一持有 Docker socket 的进程，对 worker 只开放窄接口。

为什么单独一个服务：持有 Docker socket 等于持有宿主机 root（实验：一行代码起特权容器、挂载宿主机根目录）。
worker 里跑着受模型输出影响的逻辑，所以 worker 不能拿 socket；这里的接口只能“用写死的加固参数执行代码”，
调用方改不了网络、权限、挂载这些参数。

接口（都要带 Authorization: Bearer <SANDBOX_TOKEN>）：
- POST /workspaces/{id}/exec        {code, timeout}  → 执行结果
- GET  /workspaces/{id}/files                         → 文件列表
- GET  /workspaces/{id}/file?path=…                  → 文本内容
- GET  /workspaces/{id}/file/raw?path=…              → 原始字节（交给用户下载的文件，最多 5MB）
- PUT  /workspaces/{id}/file  {path, content}         → 写入
文件路径不放进 URL 的路径部分：HTTP 客户端会把 URL 里的 .. 规范化掉，“../../<别的对话>/files/x”
会变成访问另一个工作空间（第一版就是这样，测试发现）。放在查询参数 / 请求体里，原样到达这里再校验。
启动：uvicorn agentnexus.sandbox.app:app（compose 的 sandbox 服务）
"""

import asyncio
import dataclasses
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import docker
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from agentnexus.sandbox import files
from agentnexus.sandbox.runner import DockerSandbox, Limits

MAX_TIMEOUT = 120.0
MAX_RAW_BYTES = 5 * 1024 * 1024  # 与产物上限一致（services/artifacts.py）


class ExecRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    timeout: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT)


class WriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=files.MAX_FILE_BYTES)


def create_app(sandbox: DockerSandbox, token: str, *, concurrency: int = 4) -> FastAPI:
    if not token or len(token) < 16:
        raise RuntimeError("SANDBOX_TOKEN must be set (at least 16 characters)")
    slots = asyncio.Semaphore(concurrency)  # 同时执行的容器数上限：防止一次请求风暴把机器拖垮

    def authorized(authorization: str | None = Header(default=None)):
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    def workspace(workspace_id: str) -> Path:
        try:
            return sandbox.workspace(workspace_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid workspace id") from None

    # 鉴权挂在子路由上：挂在应用级别会连健康检查一起拦（第一版这样写，路由上的 dependencies=[] 去不掉应用级依赖）
    app = FastAPI(title="AgentNexus sandbox")
    api = APIRouter(dependencies=[Depends(authorized)])

    @app.get("/health")  # 不需要令牌：只说明进程活着
    async def health():
        return {"status": "ok"}

    @api.post("/workspaces/{workspace_id}/exec")
    async def execute(workspace_id: str, body: ExecRequest):
        workspace(workspace_id)
        async with slots:
            result = await asyncio.to_thread(sandbox.execute, workspace_id, body.code, body.timeout)
        return dataclasses.asdict(result)

    @api.get("/workspaces/{workspace_id}/files")
    async def list_files(workspace_id: str):
        return files.list_files(workspace(workspace_id))

    @api.get("/workspaces/{workspace_id}/file")
    async def read_file(workspace_id: str, path: str):
        try:
            return {"path": path, "content": files.read_text(workspace(workspace_id), path)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found") from None
        except files.PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @api.get("/workspaces/{workspace_id}/file/raw")
    async def read_raw(workspace_id: str, path: str):
        try:
            data = files.read_bytes(workspace(workspace_id), path, MAX_RAW_BYTES)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found") from None
        except files.PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return Response(data, media_type="application/octet-stream")

    @api.put("/workspaces/{workspace_id}/file")
    async def write_file(workspace_id: str, body: WriteRequest):
        try:
            return {"path": body.path, "bytes": files.write_text(workspace(workspace_id), body.path, body.content)}
        except files.PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    app.include_router(api)
    return app


def _from_env() -> FastAPI:
    limits = Limits(memory=os.environ.get("SANDBOX_MEMORY", "256m"), cpus=float(os.environ.get("SANDBOX_CPUS", 0.5)))
    sandbox = DockerSandbox(
        docker.from_env(), os.environ.get("SANDBOX_IMAGE", "agentnexus-sandbox-runtime"),
        Path(os.environ.get("SANDBOX_ROOT", "/workspaces")), volume=os.environ.get("SANDBOX_VOLUME") or None,
        limits=limits,
    )
    return create_app(sandbox, os.environ.get("SANDBOX_TOKEN", ""),
                      concurrency=int(os.environ.get("SANDBOX_CONCURRENCY", 4)))


@asynccontextmanager
async def _noop(app):
    yield


app = _from_env() if os.environ.get("SANDBOX_TOKEN") else FastAPI(lifespan=_noop)  # 导入本模块做测试时不需要环境变量
