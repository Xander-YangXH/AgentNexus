"""请求追踪、访问日志与统一错误格式（decisions/0011）。"""

import asyncio
import json
import logging
import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.agents.types import RunContext
from agentnexus.observability.context import request_id_var, run_id_var
from agentnexus.observability.logging import configure_logging
from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.repositories.runs import AgentRunRepository
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.runs import RunService
from agentnexus.storage.postgres import RunStatus
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import install_test_auth


@pytest_asyncio.fixture
async def app_client(sessionmaker):
    app = create_app(with_lifespan=False)
    install_test_auth(app)

    async def boom():
        raise RuntimeError("secret internal detail: db password is hunter2")

    app.add_api_route("/test-boom", boom)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: RunDispatcher(sessionmaker, lambda ctx: None)
    # raise_app_exceptions=False：让 500 像在真实服务器里一样变成响应，而不是在测试里直接抛出
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _user(c):
    return {"X-User-Id": (await c.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]}


# ---------- request id ----------

async def test_request_id_is_generated_and_returned(app_client):
    r = await app_client.get("/health")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and int(rid, 16) >= 0


@pytest.mark.parametrize(
    "incoming, kept",
    # 非 ASCII 的值 httpx 客户端会在发请求前就报错，测不到服务端，所以只用 ASCII 范围内的不安全值
    [("abc-123.DEF_9", True), ("has space", False), ("x" * 65, False), ("a/b;c", False), ("<script>", False)],
)
async def test_incoming_request_id_is_kept_only_if_safe(app_client, incoming, kept):
    r = await app_client.get("/health", headers={"X-Request-ID": incoming})
    assert (r.headers["x-request-id"] == incoming) is kept


# ---------- 统一错误格式 ----------

def _err(r):
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["request_id"] == r.headers["x-request-id"]  # 用户报错时提供这个编号即可定位日志
    return body["error"]


async def test_error_format_for_each_kind(app_client):
    h = await _user(app_client)
    assert _err(await app_client.get(f"/api/runs/{uuid.uuid4()}", headers=h))["code"] == "run_not_found"
    assert _err(await app_client.get("/api/conversations"))["code"] == "unauthorized"
    assert _err(await app_client.get("/api/no-such-route"))["code"] == "not_found"
    assert _err(await app_client.delete("/api/users"))["code"] == "method_not_allowed"
    name = f"dup-{uuid.uuid4().hex[:6]}"
    await app_client.post("/api/users", json={"name": name})
    assert _err(await app_client.post("/api/users", json={"name": name}))["code"] == "user_exists"


async def test_validation_error_has_details_without_echoing_input(app_client):
    r = await app_client.post("/api/users", json={"name": "x" * 500})
    err = _err(r)
    assert r.status_code == 422 and err["code"] == "validation_error"
    assert err["details"][0]["loc"] == ["body", "name"]
    assert "x" * 500 not in r.text  # 不把用户提交的内容原样回显


async def test_500_hides_internals_but_is_traceable(app_client, caplog):
    with caplog.at_level(logging.ERROR, logger="server.errors"):
        r = await app_client.get("/test-boom")
    err = _err(r)
    assert r.status_code == 500 and err == {
        "code": "internal_error",
        "message": "internal server error",
        "request_id": r.headers["x-request-id"],
    }
    assert "hunter2" not in r.text  # 内部细节不出现在响应里
    record = next(rec for rec in caplog.records if rec.name == "server.errors")
    assert record.request_id == err["request_id"] and record.exc_info  # 但日志里有完整堆栈，能按编号找到


# ---------- 访问日志 ----------

async def test_access_log_line(app_client, caplog):
    h = await _user(app_client)
    with caplog.at_level(logging.DEBUG, logger="agentnexus.access"):
        r = await app_client.get("/api/conversations?token=should-not-be-logged", headers=h)
        await app_client.get("/health")
    records = [rec for rec in caplog.records if rec.name == "agentnexus.access"]
    # 按前缀找记录：若实现错误地把查询串也记进 path，应该由下面那条断言报错，而不是在这里找不到记录
    api_rec = next(rec for rec in records if rec.path.startswith("/api/conversations"))
    assert api_rec.path == "/api/conversations"
    assert (api_rec.levelno, api_rec.method, api_rec.status) == (logging.INFO, "GET", 200)
    assert api_rec.duration_ms >= 0
    assert "should-not-be-logged" not in api_rec.getMessage()  # 不记查询串
    assert next(rec for rec in records if rec.path == "/health").levelno == logging.DEBUG  # 健康检查降级
    assert r.headers["x-request-id"]


# ---------- 日志格式与上下文 ----------

def test_json_log_line_carries_context_and_extra(capsys):
    configure_logging("INFO", "json")
    try:
        rt, ut = request_id_var.set("req-1"), run_id_var.set("run-1")
        logging.getLogger("agentnexus.test").info("hello %s", "世界", extra={"status": 200})
        request_id_var.reset(rt), run_id_var.reset(ut)
        logging.getLogger("agentnexus.test").info("explicit", extra={"request_id": "given"})
    finally:
        logging.getLogger().handlers = [h for h in logging.getLogger().handlers if h.get_name() != "agentnexus"]
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["message"] == "hello 世界" and lines[0]["status"] == 200
    assert (lines[0]["request_id"], lines[0]["run_id"]) == ("req-1", "run-1")
    assert lines[1]["request_id"] == "given" and lines[1]["run_id"] is None  # 显式传入的优先；上下文已复原


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    configure_logging()
    configure_logging()
    try:
        assert sum(h.get_name() == "agentnexus" for h in root.handlers) == 1
    finally:
        root.handlers = [h for h in root.handlers if h.get_name() != "agentnexus"]


async def test_run_id_context_reaches_the_agent_task(sessionmaker, user):
    seen = {}

    async def handler(ctx: RunContext):
        seen["run_id"] = run_id_var.get()  # Agent 在独立任务里执行，上下文由 create_task 复制过来
        return "ok"

    async with sessionmaker() as session, session.begin():
        conv = await ConversationRepository(session).create(user.id)
    run = await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, handler, sweep_interval=0.1)
    await d.start()
    try:
        for _ in range(100):
            async with sessionmaker() as session:
                if (await AgentRunRepository(session).get(run.id)).status == RunStatus.SUCCEEDED:
                    break
            await asyncio.sleep(0.02)
    finally:
        await d.stop()
    assert seen["run_id"] == str(run.id)


async def test_run_lifecycle_is_logged_with_outcome_and_duration(sessionmaker, user, caplog):
    async def boom(ctx):
        raise ValueError("bad")

    async with sessionmaker() as session, session.begin():
        conv = await ConversationRepository(session).create(user.id)
    await RunService(sessionmaker).submit(user.id, conv.id, "q")
    d = RunDispatcher(sessionmaker, boom, sweep_interval=0.1)
    with caplog.at_level(logging.INFO, logger="agentnexus.runtime.dispatcher"):
        await d.start()
        try:
            for _ in range(100):
                if any(r.getMessage().startswith("run finished") for r in caplog.records):
                    break
                await asyncio.sleep(0.02)
        finally:
            await d.stop()
    msgs = [r.getMessage() for r in caplog.records if r.name == "agentnexus.runtime.dispatcher"]
    assert "run started" in msgs and "run failed" in msgs
    finished = next(r for r in caplog.records if r.getMessage().startswith("run finished"))
    assert finished.outcome == "internal" and finished.duration_ms >= 0
