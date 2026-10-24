"""工作目录里的文件读写（由 sandbox 服务直接操作，不起容器）。路径必须落在工作空间内。"""

from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024  # 读写单个文件的上限
MAX_LIST = 500


class PathError(ValueError):
    pass


def resolve(workspace: Path, relative: str) -> Path:
    """相对路径 → 绝对路径，并确认仍在工作空间内（挡住 ../、绝对路径、指向外面的符号链接）。"""
    if not relative or relative.startswith("/") or "\x00" in relative:
        raise PathError("path must be relative to the workspace")
    target = (workspace / relative).resolve()
    if target != workspace.resolve() and workspace.resolve() not in target.parents:
        raise PathError("path escapes the workspace")
    return target


def list_files(workspace: Path) -> list[dict]:
    out = []
    for p in sorted(workspace.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append({"path": str(p.relative_to(workspace)), "size": p.stat().st_size})
            if len(out) >= MAX_LIST:
                break
    return out


def read_text(workspace: Path, relative: str) -> str:
    target = resolve(workspace, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    if target.stat().st_size > MAX_FILE_BYTES:
        raise PathError(f"file larger than {MAX_FILE_BYTES} bytes")
    data = target.read_bytes()
    if b"\x00" in data[:8192]:
        # 图片等二进制文件按文本读出来是一大段乱码，白占上下文（实测模型会去 read_file 一张刚画好的 PNG）
        raise PathError("binary file (e.g. an image) cannot be read as text; use share_file to give it to the user")
    return data.decode("utf-8", errors="replace")


def read_bytes(workspace: Path, relative: str, max_bytes: int) -> bytes:
    """原样读取（图表、表格等交给用户下载的文件）。"""
    target = resolve(workspace, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    if target.stat().st_size > max_bytes:
        raise PathError(f"file larger than {max_bytes} bytes")
    return target.read_bytes()


def write_text(workspace: Path, relative: str, content: str) -> int:
    data = content.encode()
    if len(data) > MAX_FILE_BYTES:
        raise PathError(f"content larger than {MAX_FILE_BYTES} bytes")
    target = resolve(workspace, relative)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
    target.write_bytes(data)
    target.chmod(0o666)  # 沙盒里的代码（nobody）也要能改
    return len(data)
