"""沙盒选型实验（decisions/0021 草稿）：Docker 容器加固后，“模型写的代码”能做什么、不能做什么，以及启动延迟。

用法：pip install docker；python isolation.py（需要本机 Docker）。不调用任何付费 API。
每个用例都是一段“恶意或失控”的代码，在加固后的容器里运行，记录结果。
"""

import statistics
import tempfile
import textwrap
import time
from pathlib import Path

import docker

IMAGE = "python:3.12-slim"
client = docker.from_env()

HARDENED = dict(
    network_mode="none",  # 断网：不能访问外网，也不能访问 compose 里的内部服务
    read_only=True,  # 根文件系统只读
    tmpfs={"/tmp": "rw,size=16m,noexec,nosuid"},
    user="65534:65534",  # nobody
    cap_drop=["ALL"],
    security_opt=["no-new-privileges"],
    pids_limit=64,  # 防 fork 炸弹
    mem_limit="256m",
    memswap_limit="256m",  # 不许用 swap 绕过内存限制
    nano_cpus=500_000_000,  # 0.5 核
    working_dir="/workspace",
)


def run(code: str, workspace: Path, timeout: float = 5.0) -> tuple[str, str]:
    """返回（结局, 输出摘要）。结局：exit=<码> / timeout / oom。"""
    c = client.containers.create(IMAGE, ["python", "-c", code], volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
                                 **HARDENED)
    try:
        c.start()
        try:
            res = c.wait(timeout=timeout)
            c.reload()
            outcome = "oom" if c.attrs["State"]["OOMKilled"] else f"exit={res['StatusCode']}"
        except Exception:  # 超时：requests 的 ReadTimeout
            c.kill()
            outcome = "timeout（已 kill）"
        out = c.logs().decode(errors="replace").strip().splitlines()
        return outcome, (out[-1] if out else "")[:110]
    finally:
        c.remove(force=True)


CASES = {
    "正常计算": "print(sum(range(10**6)))",
    "写工作目录": "open('/workspace/out.txt','w').write('ok'); print(open('/workspace/out.txt').read())",
    "写根文件系统": "open('/usr/evil','w').write('x')",
    "访问外网": "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2); print('connected')",
    "访问宿主机 / 内部服务": "import socket; socket.create_connection(('host.docker.internal', 5432), timeout=2); print('connected')",
    "当前用户": "import os; print('uid', os.getuid(), 'gid', os.getgid())",
    "提权（setuid 0）": "import os; os.setuid(0); print('root!')",
    "读宿主机的 Docker socket": "import os; print('docker.sock exists:', os.path.exists('/var/run/docker.sock'))",
    "fork 炸弹（限 64 个进程）": textwrap.dedent("""
        import os, time
        n = 0
        try:
            while True:
                if os.fork() == 0:
                    time.sleep(30); os._exit(0)
                n += 1
        except OSError as e:
            print('fork stopped after', n, ':', e)
    """),
    "内存炸弹（限 256MB）": "x = bytearray(1024*1024*1024); print('allocated 1GB')",
    "死循环（5 秒超时）": "while True: pass",
}


def latency(workspace: Path):
    cold = []
    for _ in range(8):
        t = time.perf_counter()
        run("print(1)", workspace)
        cold.append((time.perf_counter() - t) * 1000)
    warm_c = client.containers.run(IMAGE, ["sleep", "300"], detach=True,
                                   volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}}, **HARDENED)
    warm = []
    try:
        for _ in range(8):
            t = time.perf_counter()
            warm_c.exec_run(["python", "-c", "print(1)"])
            warm.append((time.perf_counter() - t) * 1000)
    finally:
        warm_c.remove(force=True)
    print(f"\n延迟：每次新建容器（create+start+wait+remove）中位数 {statistics.median(cold):.0f}ms；"
          f"复用容器 exec 中位数 {statistics.median(warm):.0f}ms")


def main():
    print(f"Docker {client.version()['Version']}，镜像 {IMAGE}")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        ws.chmod(0o777)  # 容器里是 nobody，需要能写工作目录
        for name, code in CASES.items():
            t = time.perf_counter()
            outcome, out = run(code, ws, timeout=5.0 if "死循环" in name else 15.0)
            print(f"{name:<24} {outcome:<16} {time.perf_counter() - t:5.1f}s  {out}")
        latency(ws)


if __name__ == "__main__":
    main()
