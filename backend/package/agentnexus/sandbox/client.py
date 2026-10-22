"""worker 调用 sandbox 服务的客户端（decisions/0021）。"""

import uuid

import httpx


class SandboxError(RuntimeError):
    pass


class SandboxClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 150.0, transport=None):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                       headers={"Authorization": f"Bearer {token}"})

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _ws(workspace_id: str) -> str:
        # 拼进 URL 之前先校验：工作空间 id 只能是 UUID（客户端会规范化 URL 里的 ..，不能指望服务端看到原样）
        return str(uuid.UUID(workspace_id))

    async def _call(self, method: str, url: str, **kw) -> dict | list:
        r = await self._http.request(method, url, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text[:200]
            raise SandboxError(f"{r.status_code}: {detail}")
        return r.json()

    async def execute(self, workspace_id: str, code: str, timeout: float = 30.0) -> dict:
        return await self._call("POST", f"/workspaces/{self._ws(workspace_id)}/exec", json={"code": code, "timeout": timeout})

    async def list_files(self, workspace_id: str) -> list[dict]:
        return await self._call("GET", f"/workspaces/{self._ws(workspace_id)}/files")

    # 路径（模型给的）只放在查询参数 / 请求体里，不拼进 URL 路径：避免客户端规范化 .. 后跨到别的工作空间
    async def read_file(self, workspace_id: str, path: str) -> str:
        return (await self._call("GET", f"/workspaces/{self._ws(workspace_id)}/file", params={"path": path}))["content"]

    async def write_file(self, workspace_id: str, path: str, content: str) -> int:
        body = {"path": path, "content": content}
        return (await self._call("PUT", f"/workspaces/{self._ws(workspace_id)}/file", json=body))["bytes"]
