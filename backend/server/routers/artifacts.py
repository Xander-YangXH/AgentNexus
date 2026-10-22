import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response

from agentnexus.services.artifacts import ArtifactService
from server.deps import CurrentUser, get_artifact_service

router = APIRouter(prefix="/api", tags=["artifacts"])


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID, user_id: CurrentUser, svc: ArtifactService = Depends(get_artifact_service)
):
    """下载 Agent 产物（decisions/0022）。内容来自模型：一律作为附件下载、禁止浏览器猜测类型，不在页面里当 HTML 渲染。"""
    artifact, data = await svc.get(user_id, artifact_id)
    ascii_name = artifact.filename.encode("ascii", "ignore").decode() or "download"
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            # 中文文件名用 RFC 5987 的 filename*；filename 给不支持它的客户端一个 ASCII 兜底
            "Content-Disposition": f"attachment; filename=\"{ascii_name.replace('\"', '')}\"; "
                                   f"filename*=UTF-8''{quote(artifact.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )
