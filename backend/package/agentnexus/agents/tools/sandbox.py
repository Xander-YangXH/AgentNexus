"""沙盒工具（decisions/0021）：模型写的代码和文件操作，都经 sandbox 服务在加固的一次性容器 / 工作空间里执行。

工作空间按对话划分：同一对话的后续提问（“再画个图”）能用上之前的文件；别的对话看不到。
"""

from agentnexus.agents.tools import AgentTool, ToolError, ToolScope, truncate
from agentnexus.sandbox.client import SandboxClient, SandboxError

PYTHON_EXEC = "python_exec"


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


def sandbox_tools(client: SandboxClient, *, exec_timeout: float = 30.0) -> list[AgentTool]:
    def scoped(fn):
        async def run(args: dict, scope: ToolScope | None) -> str:
            if scope is None:
                raise ToolError("缺少工作空间")
            try:
                return truncate(await fn(scope.workspace_id, args))
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

    path = {"type": "string", "description": "相对于工作目录的路径，如 data/sales.csv"}
    return [
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
