"""沙盒工具（decisions/0021）：模型写的代码和文件操作，都经 sandbox 服务在加固的一次性容器 / 工作空间里执行。

工作空间按对话划分：同一对话的后续提问（“再画个图”）能用上之前的文件；别的对话看不到。
share_file（阶段 7.2）：把工作目录里的文件复制成产物，作为回复的附件交给用户下载。由 worker 取文件、写产物，
api 不需要沙盒的令牌（每个服务只拿自己需要的权限，0021）。
"""

import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

from agentnexus.agents.tools import AgentTool, ToolError, ToolOutput, ToolScope, truncate
from agentnexus.sandbox.client import SandboxClient, SandboxError

PYTHON_EXEC = "python_exec"
SHARE_FILE = "share_file"
# 保留原类型的文件（下载时浏览器知道是什么）；其余一律按二进制下载。不保留 HTML / SVG：
# 下载接口虽然强制 attachment，但它们是能执行脚本的格式，没必要冒险
KEEP_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "text/csv", "text/plain", "text/markdown",
              "application/json", "application/pdf",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

SaveArtifact = Callable[[str, str, bytes], Awaitable[dict]]


def content_type_of(filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0] or ""
    if filename.lower().endswith(".md"):
        guessed = "text/markdown"
    if guessed not in KEEP_TYPES:
        return "application/octet-stream"
    return f"{guessed}; charset=utf-8" if guessed.startswith("text/") else guessed


def _format_exec(result: dict) -> str:
    if result.get("timed_out"):
        status = "超时，已被终止"
    elif result.get("oom"):
        status = "内存不足，已被终止"
    else:
        status = f"退出码 {result.get('exit_code')}"
    parts = [status]
    for key in ("stdout", "stderr"):
        if result.get(key):
            parts.append(f"{key}:\n{result[key]}")
    if len(parts) == 1:
        parts.append("（没有输出：用 print 打印需要的结果）")
    return "\n".join(parts)


def sandbox_tools(client: SandboxClient, *, exec_timeout: float = 30.0,
                  save_artifact: SaveArtifact | None = None) -> list[AgentTool]:
    def scoped(fn):
        async def run(args: dict, scope: ToolScope | None) -> str | ToolOutput:
            if scope is None:
                raise ToolError("缺少工作空间")
            try:
                result = await fn(scope.workspace_id, args)
                return result if isinstance(result, ToolOutput) else truncate(result)
            except SandboxError as exc:
                raise ToolError(f"沙盒拒绝或执行失败（{exc}）") from None
            except KeyError as exc:
                raise ToolError(f"缺少参数 {exc}") from None
        return run

    async def python_exec(ws, args):
        return _format_exec(await client.execute(ws, str(args["code"]), timeout=exec_timeout))

    async def list_files(ws, args):
        files = await client.list_files(ws)
        return "\n".join(f"{f['path']}（{f['size']} 字节）" for f in files) or "工作目录是空的。"

    async def read_file(ws, args):
        return await client.read_file(ws, str(args["path"]))

    async def write_file(ws, args):
        size = await client.write_file(ws, str(args["path"]), str(args["content"]))
        return f"已写入 {args['path']}（{size} 字节）"

    async def share_file(ws, args):
        path = str(args["path"])
        data = await client.read_bytes(ws, path)
        name = PurePosixPath(path).name
        attachment = await save_artifact(name, content_type_of(name), data)
        return ToolOutput(f"已把 {name}（{len(data)} 字节）作为附件交给用户，回答里提到文件名即可，不要写下载链接或路径。",
                          [attachment])

    path = {"type": "string", "description": "相对于工作目录的路径，如 data/sales.csv"}
    tools = [
        AgentTool(PYTHON_EXEC,
                  f"在隔离的沙盒里执行 Python 3.12 代码（预装 pandas、numpy、matplotlib；没有网络）。"
                  f"当前目录就是本对话的工作目录，文件在多次调用之间保留；图表用 matplotlib 保存成文件。"
                  f"每次最多运行 {exec_timeout:g} 秒，结果要用 print 输出。",
                  {"type": "object", "properties": {"code": {"type": "string", "description": "完整的 Python 代码"}},
                   "required": ["code"]},
                  scoped(python_exec)),
        AgentTool("list_files", "列出本对话工作目录里的文件。", {"type": "object", "properties": {}},
                  scoped(list_files)),
        AgentTool("read_file", "读取工作目录里的文本文件（最多 1MB）。",
                  {"type": "object", "properties": {"path": path}, "required": ["path"]}, scoped(read_file)),
        AgentTool("write_file", "把文本写入工作目录里的文件（覆盖；最多 1MB）。",
                  {"type": "object", "properties": {"path": path, "content": {"type": "string"}},
                   "required": ["path", "content"]},
                  scoped(write_file)),
    ]
    if save_artifact is not None:
        tools.append(AgentTool(SHARE_FILE, "把工作目录里的文件（图表、表格、报告，最多 5MB）作为附件交给用户下载。"
                                           "用户需要拿到的文件都要用它交付，用户看不到工作目录。",
                               {"type": "object", "properties": {"path": path}, "required": ["path"]},
                               scoped(share_file)))
    return tools
