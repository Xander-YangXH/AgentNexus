"""实验：拿到 Docker socket 的程序能做什么（decisions/0021 草稿）。

这正是“把 /var/run/docker.sock 挂进 worker”的风险：worker 里跑着受模型输出影响的逻辑，一旦被提示注入诱导，
它能创建任意参数的容器——包括特权容器、挂载宿主机根目录。这里只读取 Docker 虚拟机里的两个无害文件来证明。
"""

import docker

client = docker.from_env()
out = client.containers.run(
    "python:3.12-slim", ["sh", "-c", "id; cat /host/etc/hostname; ls /host/var/lib/docker | head -3"],
    volumes={"/": {"bind": "/host", "mode": "ro"}}, privileged=True, remove=True,
)
print(out.decode())
print("结论：能挂载宿主机根目录、以特权模式运行——持有 socket 等于持有（Docker 所在机器的）root。")

# volume subpath：worker 在容器里时，按 run 隔离工作目录的办法（不依赖宿主机路径）
from docker.types import Mount

client.volumes.create("agentnexus_sandbox_exp")
try:
    client.containers.run("python:3.12-slim", ["sh", "-c", "mkdir -p /v/run-a && chmod 777 /v/run-a && echo secret-b > /v/other.txt"],
                          mounts=[Mount("/v", "agentnexus_sandbox_exp", type="volume")], remove=True)
    res = client.containers.run(
        "python:3.12-slim", ["sh", "-c", "echo hi > /workspace/f.txt; ls -la /workspace; cat /workspace/../other.txt 2>&1 | head -1"],
        mounts=[Mount("/workspace", "agentnexus_sandbox_exp", type="volume", subpath="run-a")],
        user="65534:65534", read_only=True, network_mode="none", remove=True,
    )
    print("volume subpath：", res.decode().strip().replace("\n", " | "))
finally:
    client.volumes.get("agentnexus_sandbox_exp").remove(force=True)
