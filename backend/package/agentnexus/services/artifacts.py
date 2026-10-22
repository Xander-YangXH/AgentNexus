"""Agent 产物（decisions/0022）：例如调研报告。

- 写：Agent 先把内容写进原件存储（键 artifacts/<id>），再由派发器在“完成 run”的同一事务里写元数据（带租约校验）；
- 读：按用户校验归属，不是自己的与不存在同样报 404。
"""

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.services.errors import ArtifactNotFound
from agentnexus.storage.blobs import BlobNotFound, BlobStore
from agentnexus.storage.postgres import Artifact

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024


def blob_key(artifact_id: uuid.UUID) -> str:
    return f"artifacts/{artifact_id}"


class ArtifactService:
    def __init__(self, sessionmaker: async_sessionmaker, blobs: BlobStore):
        self.sessionmaker = sessionmaker
        self.blobs = blobs

    async def save(self, filename: str, content_type: str, data: bytes) -> dict:
        """写原件，返回交给 AgentReply.attachments 的描述（元数据由 RunService.complete 落库）。"""
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact larger than {MAX_ARTIFACT_BYTES} bytes")
        artifact_id = uuid.uuid4()
        await self.blobs.put(blob_key(artifact_id), data)
        return {"id": str(artifact_id), "filename": filename, "content_type": content_type, "size": len(data),
                "blob_key": blob_key(artifact_id)}

    async def get(self, user_id: uuid.UUID, artifact_id: uuid.UUID) -> tuple[Artifact, bytes]:
        async with self.sessionmaker() as session:
            artifact = await session.get(Artifact, artifact_id)
        if artifact is None or artifact.user_id != user_id:
            raise ArtifactNotFound(str(artifact_id))
        try:
            return artifact, await self.blobs.get(artifact.blob_key)
        except BlobNotFound:
            raise ArtifactNotFound(str(artifact_id)) from None
