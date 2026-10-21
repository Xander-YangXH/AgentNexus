"""原始文件存储（阶段 2，decisions/0015）。

为什么不放进数据库：大文件会拖慢备份、事务和复制，数据库只存元数据和解析后的文本。
为什么先用本地数据卷：少一个服务；接口与 S3 / MinIO 一致（按 key 存取），以后换实现不影响调用方。
"""

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

# key 只允许安全字符：调用方传入的 key 不能通过 ../ 之类逃出存储根目录
_SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9/_.-]{0,250}$")


class BlobNotFound(Exception):
    pass


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _SAFE_KEY.match(key) or ".." in key.split("/"):
            raise ValueError(f"unsafe blob key: {key!r}")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError(f"unsafe blob key: {key!r}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        await asyncio.to_thread(_atomic_write, path, data)  # 文件 IO 放到线程里，不阻塞事件循环

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise BlobNotFound(key) from exc

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


def _atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再改名：进程中途崩溃也不会留下“写了一半”的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
