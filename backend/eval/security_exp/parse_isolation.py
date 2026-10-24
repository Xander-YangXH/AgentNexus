"""实验 3：解析不可信文件时，“卡死 / 吃光内存”的影响能不能被限制住（decisions/0025 草稿）。

pypdf 6.19.0 有 11 个已知漏洞，多数是“构造的 PDF 让解析陷入死循环或占用大量内存”。这里不复现具体漏洞，
而是用两个模拟的“恶意解析”（死循环、无限分配内存）比较两种执行方式：
  A. 现在的做法：asyncio.to_thread 在线程里解析；
  B. 子进程解析：单独的进程 + 超时 + 内存上限（RLIMIT_AS，只在 Linux 生效），超时就杀掉。
还测一个正常 20 页 PDF 两种方式的耗时（子进程的额外开销）。
必须在 Linux 里跑（worker 容器），而且要是文件（forkserver 的子进程要导入主模块，标准输入传进去的脚本导入不了）：
  docker compose cp parse_isolation.py worker:/tmp/ && docker compose exec -T -w /tmp worker python parse_isolation.py
"""

import io
import multiprocessing as mp
import resource
import subprocess
import sys
import time

from agentnexus.knowledge.parsing import parse


def make_pdf(pages):
    """手工拼一个最小的合法 PDF（与 tests/test_parsing.py 相同的做法）。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content.decode()}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def hang(_data):  # 模拟“构造的 PDF 让解析陷入死循环”
    while True:
        pass


def bomb(_data):  # 模拟“构造的 PDF 让解析无限占用内存”
    chunks = []
    while True:
        chunks.append(bytearray(50 * 1024 * 1024))


def real(data):
    return len(parse("doc.pdf", data))


def _child(fn, data, mem_mb, conn):
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
    try:
        conn.send(("ok", fn(data)))
    except MemoryError:
        conn.send(("memory", None))


def run_isolated(fn, data, timeout=3.0, mem_mb=512):
    ctx = mp.get_context("forkserver")
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(fn, data, mem_mb, child), daemon=True)
    p.start()
    child.close()
    ready = parent.poll(timeout)
    if not ready:
        p.kill()
        p.join()
        return "timeout(killed)"
    try:
        result = parent.recv()
    except EOFError:  # 子进程没回结果就退出了（例如被系统杀掉）
        result = ("died", None)
    p.join()
    return result[0] if result[0] != "ok" else result[1]


def main():
    pdf = make_pdf([f"Return policy page {i}" for i in range(20)])
    print(f"正常 PDF：{len(pdf)} 字节、20 页")
    t = time.perf_counter()
    n = real(pdf)
    a = time.perf_counter() - t
    t = time.perf_counter()
    m = run_isolated(real, pdf)
    b = time.perf_counter() - t
    print(f"  线程（直接调用）：{n} 段，{a * 1000:.0f}ms；子进程：{m} 段，{b * 1000:.0f}ms（首次，含 forkserver 启动）")
    t = time.perf_counter()
    run_isolated(real, pdf)
    b2 = time.perf_counter() - t
    print(f"  子进程再次：{b2 * 1000:.0f}ms")

    print("A. 线程里解析（现在的做法）")
    # 只在独立进程里测：asyncio.run 退出时要等默认线程池里的线程结束，停不下来的线程会让进程一直退不出去
    # （第一版直接在本进程里 asyncio.run，实验自己卡住了 5 分钟——这正是要证明的现象）
    code = ("import asyncio, sys; sys.path.insert(0, '.')\n"
            "def hang():\n    while True: pass\n"
            "async def m():\n    try:\n        await asyncio.wait_for(asyncio.to_thread(hang), 1)\n    except TimeoutError:\n        print('timeout', flush=True)\n"
            "asyncio.run(m())\nprint('exited', flush=True)\n")
    t = time.perf_counter()
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.split()
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode().split() + ["(30 秒后仍未退出，被强制结束)"]
    print(f"  死循环：wait_for 超时后 → {out}，{time.perf_counter() - t:.1f}s")

    print("B. 子进程解析（超时 3 秒、内存上限 512MB）")
    t = time.perf_counter()
    r = run_isolated(hang, pdf)
    print(f"  死循环：{r}，{time.perf_counter() - t:.1f}s")
    t = time.perf_counter()
    r = run_isolated(bomb, pdf)
    print(f"  内存炸弹：{r}，{time.perf_counter() - t:.1f}s")


if __name__ == "__main__":
    main()
