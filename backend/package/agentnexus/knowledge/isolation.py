"""在子进程里解析不可信的文件（decisions/0025）。

PDF、Word 的解析库（pypdf、python-docx）处理的是用户上传的、可能被构造过的文件；pypdf 6.19 就有 11 个
“构造的 PDF 导致死循环 / 内存耗尽”的漏洞。在线程里解析时，一旦卡死：
- 线程停不下来（Python 不能杀线程），心跳照常续约，文档永远停在“处理中”，也不会被别的 worker 接管；
- 入库执行槽一直被占着，之后上传的文档全部排队；优雅关停时进程也退不出去。
实验（eval/security_exp/parse_isolation.py，worker 容器里）：子进程 + 超时 + 内存上限，死循环 3 秒后被杀、
内存炸弹 0.3 秒后被拦；代价是每篇多约 40~150ms 的进程启动。

- 用 forkserver 启动子进程：worker 是多线程的异步进程，直接 fork 不安全；spawn 每次都要重新导入，更慢；
- 内存上限用 RLIMIT_AS，只在 Linux 上生效（macOS 上设置会失败，只剩超时这一层）；
- 纯文本（.txt / .md）只是解码，不走子进程。
"""

import contextlib
import multiprocessing as mp
import resource
from collections.abc import Callable

from agentnexus.knowledge.parsing import ParseError, Section, extension_of, parse

ISOLATED_EXTENSIONS = {".pdf", ".docx"}
_ctx = mp.get_context("forkserver")
_ctx.set_forkserver_preload(["agentnexus.knowledge.parsing"])  # 预先导入解析库：每次只需 fork，不必重新导入 pypdf

Parser = Callable[[str, bytes], list[Section]]


def _child(parser: Parser, filename: str, data: bytes, mem_mb: int, conn) -> None:
    with contextlib.suppress(ValueError, OSError):  # macOS 不支持限制地址空间：只靠超时
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
    try:
        conn.send(("ok", parser(filename, data)))
    except ParseError as exc:
        conn.send(("parse_error", (exc.code, str(exc))))
    except MemoryError:
        conn.send(("memory", None))
    except BaseException as exc:  # noqa: BLE001 —— 子进程里任何异常都要报回去，不能让父进程只看到“连接断了”
        conn.send(("error", f"{type(exc).__name__}: {exc}"))


def parse_isolated(filename: str, data: bytes, *, timeout: float = 60.0, mem_mb: int = 1024,
                   parser: Parser = parse) -> list[Section]:
    """语义与 parse() 相同（成功返回段落，失败抛 ParseError），只是在子进程里执行、有超时和内存上限。

    parser 必须是模块级函数（子进程要能按名字导入它）；测试里用它注入“恶意”的解析函数。
    """
    if extension_of(filename) not in ISOLATED_EXTENSIONS:
        return parser(filename, data)
    receiver, sender = _ctx.Pipe(duplex=False)
    process = _ctx.Process(target=_child, args=(parser, filename, data, mem_mb, sender), daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            raise ParseError("parse_timeout", f"解析超过 {timeout:g} 秒，已终止（文件过于复杂，或者是构造的恶意文件）")
        try:
            status, payload = receiver.recv()
        except EOFError:
            raise ParseError("parse_failed", "解析进程异常退出（可能占用了过多内存）") from None
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        receiver.close()
    if status == "ok":
        return payload
    if status == "parse_error":
        code, message = payload
        raise ParseError(code, message)
    if status == "memory":
        raise ParseError("too_complex", f"解析时占用内存超过 {mem_mb}MB，已终止（文件过于复杂，或者是构造的恶意文件）")
    raise ParseError("corrupt", f"文件无法解析：{payload}")
