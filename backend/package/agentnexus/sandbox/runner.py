"""在加固的一次性容器里执行代码（decisions/0021 的实验配置）。

每次执行都新建一个容器（实测创建到删除中位数 126ms），不复用：不会残留进程，上一次执行影响不到下一次。
同一个工作空间（一个对话）的文件放在工作目录里，跨执行保留。

工作目录的两种挂载方式：
- volume：sandbox 服务自己也在容器里时，传给 Docker 的路径会被当成宿主机路径。所以用命名卷的子目录挂载
  （volume subpath），每个工作空间只挂自己的子目录；
- bind：本机开发 / 测试时，直接挂宿主机上的目录。
"""

import dataclasses
import re
import time
import uuid
from pathlib import Path

import docker
from docker.types import Mount

OUTPUT_LIMIT = 8000  # stdout / stderr 各自最多返回多少字符：避免把超大输出塞进模型上下文
_WORKSPACE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclasses.dataclass(frozen=True)
class Limits:
    memory: str = "256m"
    cpus: float = 0.5
    pids: int = 64
    tmpfs_size: str = "16m"


@dataclasses.dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    oom: bool
    duration_ms: float


def _clip(text: str) -> str:
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + f"\n…（输出过长，已截断，共 {len(text)} 字符）"


class DockerSandbox:
    def __init__(self, client: docker.DockerClient, image: str, root: Path, *, volume: str | None = None,
                 limits: Limits = Limits()):
        self.client = client
        self.image = image
        self.root = root  # 工作目录在本服务看到的位置（volume 模式下是该卷在本容器里的挂载点）
        self.volume = volume
        self.limits = limits

    def workspace(self, workspace_id: str) -> Path:
        """工作空间 id 只能是 UUID（对话 id）：拼进路径和卷子目录之前先校验，杜绝 ../ 之类。"""
        if not _WORKSPACE_ID.match(workspace_id):
            raise ValueError("invalid workspace id")
        path = self.root / workspace_id
        path.mkdir(mode=0o777, exist_ok=True)
        path.chmod(0o777)  # 容器里是 nobody，需要能写
        return path

    def _mount(self, workspace_id: str) -> Mount:
        if self.volume:
            return Mount("/workspace", self.volume, type="volume", subpath=workspace_id)
        return Mount("/workspace", str(self.root / workspace_id), type="bind")

    def execute(self, workspace_id: str, code: str, timeout: float) -> ExecResult:
        """同步执行（Docker SDK 是同步的），调用方放到线程里。"""
        self.workspace(workspace_id)
        lim = self.limits
        started = time.perf_counter()
        container = self.client.containers.create(
            self.image, ["python", "-c", code],
            name=f"anx-sandbox-{uuid.uuid4().hex[:12]}",
            mounts=[self._mount(workspace_id)],
            working_dir="/workspace",
            network_mode="none",  # 断网：碰不到外网，也碰不到没有鉴权的内部服务
            read_only=True,
            tmpfs={"/tmp": f"rw,size={lim.tmpfs_size},noexec,nosuid"},
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=lim.pids,
            mem_limit=lim.memory,
            memswap_limit=lim.memory,  # 不许用 swap 绕过内存限制
            nano_cpus=int(lim.cpus * 1e9),
            environment={"MPLBACKEND": "Agg", "HOME": "/tmp"},  # matplotlib 无界面后端；HOME 指向可写的 tmpfs
            labels={"agentnexus.sandbox": "1"},
        )
        timed_out = False
        try:
            container.start()
            try:
                exit_code = container.wait(timeout=timeout)["StatusCode"]
            except Exception:  # requests 的 ReadTimeout：超时
                timed_out, exit_code = True, None
                container.kill()
            container.reload()
            oom = bool(container.attrs["State"].get("OOMKilled"))
            stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
        finally:
            container.remove(force=True)
        return ExecResult(exit_code=exit_code, stdout=_clip(stdout), stderr=_clip(stderr), timed_out=timed_out, oom=oom,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1))
