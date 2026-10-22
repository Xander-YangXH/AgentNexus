"""沙盒服务（阶段 4.2，decisions/0021）：鉴权、执行、隔离、工作目录与路径校验。

用本机真实的 Docker 起容器（没有 Docker 时跳过）。生产用的运行镜像预装了 pandas 等，这里用 python:3.12-slim 即可。
"""

import os
import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.sandbox import files
from agentnexus.sandbox.app import create_app
from agentnexus.sandbox.client import SandboxClient, SandboxError

TOKEN = "test-token-0123456789abcdef"


@pytest.fixture(scope="module")
def docker_client():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get("python:3.12-slim")
    except Exception as exc:
        pytest.skip(f"需要本机 Docker 和 python:3.12-slim 镜像：{type(exc).__name__}")
    return client


@pytest_asyncio.fixture
async def sandbox(docker_client, tmp_path):
    from agentnexus.sandbox.runner import DockerSandbox

    root = tmp_path / "workspaces"
    root.mkdir()
    box = DockerSandbox(docker_client, "python:3.12-slim", root)
    app = create_app(box, TOKEN)
    client = SandboxClient("http://sandbox", TOKEN, transport=httpx.ASGITransport(app=app))
    yield client, app, root
    await client.close()


def _ws():
    return str(uuid.uuid4())


async def test_requires_the_token_but_health_does_not(sandbox):
    _, app, _ = sandbox
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://s") as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get(f"/workspaces/{_ws()}/files")).status_code == 401
        bad = {"Authorization": "Bearer wrong-token-xxxxxxxxxxxx"}
        assert (await c.post(f"/workspaces/{_ws()}/exec", json={"code": "print(1)"}, headers=bad)).status_code == 401
    with pytest.raises(RuntimeError):
        create_app(None, "short")  # 令牌太短拒绝启动


async def test_executes_code_and_reports_output_and_failures(sandbox):
    client, _, _ = sandbox
    ws = _ws()
    ok = await client.execute(ws, "print('hello'); import sys; print('warn', file=sys.stderr)")
    assert (ok["exit_code"], ok["stdout"], ok["stderr"], ok["timed_out"]) == (0, "hello\n", "warn\n", False)
    bad = await client.execute(ws, "raise ValueError('boom')")
    assert bad["exit_code"] == 1 and "ValueError: boom" in bad["stderr"]
    slow = await client.execute(ws, "while True: pass", timeout=1)
    assert slow["timed_out"] and slow["exit_code"] is None
    net = await client.execute(ws, "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)")
    assert net["exit_code"] == 1 and "Network is unreachable" in net["stderr"]
    who = await client.execute(ws, "import os; print(os.getuid())")
    assert who["stdout"].strip() == "65534"


async def test_files_are_shared_between_code_and_api_and_isolated_per_workspace(sandbox):
    client, _, root = sandbox
    a, b = _ws(), _ws()
    await client.write_file(a, "data/input.csv", "x,y\n1,2\n")
    out = await client.execute(a, "print(open('data/input.csv').read().splitlines()[1]); open('result.txt','w').write('42')")
    assert out["stdout"].strip() == "1,2"
    assert await client.read_file(a, "result.txt") == "42"
    assert [f["path"] for f in await client.list_files(a)] == ["data/input.csv", "result.txt"]
    assert await client.list_files(b) == []  # 另一个对话的工作空间看不到
    probe = await client.execute(b, "import os; print(os.listdir('/workspace'), os.path.exists('/workspace/../' + 'x'))")
    assert probe["stdout"].startswith("[]")


async def test_path_and_workspace_validation(sandbox):
    client, _, root = sandbox
    ws = _ws()
    for bad in ("../escape.txt", "/etc/passwd", "a/../../escape.txt"):
        with pytest.raises(SandboxError, match="400"):
            await client.write_file(ws, bad, "x")
    with pytest.raises(ValueError):
        await client.execute("../../etc", "print(1)")  # 客户端先拒绝：不让它进 URL
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox[1]), base_url="http://s",
                                 headers={"Authorization": f"Bearer {TOKEN}"}) as raw:
        assert (await raw.post("/workspaces/not-a-uuid/exec", json={"code": "print(1)"})).status_code == 400
    # 沙盒里的代码建一个指向工作空间外的符号链接，再通过接口读：必须被拒
    outside = root.parent / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, root / ws / "link.txt")
    with pytest.raises(SandboxError, match="400"):
        await client.read_file(ws, "link.txt")
    with pytest.raises(SandboxError, match="404"):
        await client.read_file(ws, "missing.txt")


async def test_cannot_reach_another_workspace_through_dot_segments(sandbox):
    """第一版把文件路径拼进 URL：HTTP 客户端会规范化 ..，"../../<别的对话>/files/x" 就成了读另一个工作空间的文件。"""
    client, _, _ = sandbox
    victim, attacker = _ws(), _ws()
    await client.write_file(victim, "secret.txt", "victim data")
    for path in (f"../{victim}/secret.txt", f"../../{victim}/files/secret.txt", f"../../workspaces/{victim}/file?path=secret.txt"):
        with pytest.raises(SandboxError, match="400|404"):
            got = await client.read_file(attacker, path)
            pytest.fail(f"read another workspace via {path!r}: {got!r}")


def test_resolve_rejects_escapes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert files.resolve(ws, "a/b.txt") == (ws / "a" / "b.txt").resolve()
    for bad in ("", "/abs", "../x", "a/../../x", "a\x00b"):
        with pytest.raises(files.PathError):
            files.resolve(ws, bad)
