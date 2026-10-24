"""运行状态（阶段 7.8，decisions/0031）：从业务表聚合；api 角色受 RLS 约束也能看到全局计数（且只有计数）；
管理员接口与 Prometheus 文本格式。测试库是共用的，所以都按“插入前后的差值”断言。"""

import uuid

import httpx
from sqlalchemy import func, select

from agentnexus.security.principal import Principal, principal_var
from agentnexus.services.status import prometheus, system_status
from agentnexus.storage.postgres import AgentRun, Document, Message, PurgeJob
from server.deps import get_sessionmaker
from server.main import create_app
from tests.fakes import install_test_auth
from tests.test_tenancy import org, role_sessionmaker, role_urls  # noqa: F401 —— 复用部门、用户与数据库角色的夹具


async def _backlog(sessionmaker, org):  # noqa: F811
    """alice 的对话里一个排队的 run，她的共享库里一篇失败的文档，外加一条重试过两次的清理任务。"""
    async with sessionmaker() as s, s.begin():
        msg = Message(conversation_id=org["conv"], role="user", content="排队中的问题")
        s.add(msg)
        await s.flush()
        s.add(AgentRun(conversation_id=org["conv"], input_message_id=msg.id, status="queued"))
        s.add(Document(id=uuid.uuid4(), knowledge_base_id=org["shared"], filename="坏.pdf", size_bytes=1,
                       sha256=uuid.uuid4().hex * 2, blob_key="documents/x", status="failed", chunk_count=0))
        s.add(PurgeJob(kind="document", knowledge_base_id=org["shared"], document_id=uuid.uuid4(), blob_keys=[],
                       attempts=2))


async def test_status_counts_everything_even_for_a_role_that_rls_restricts(org, sessionmaker, role_sessionmaker):  # noqa: F811
    api_sm = role_sessionmaker("api")
    bob = org["users"]["bob"]
    token = principal_var.set(Principal(bob.id, bob.name, "member", bob.department_id))  # 以 bob 的身份连接
    try:
        before = await system_status(api_sm)
        await _backlog(sessionmaker, org)
        after = await system_status(api_sm)
        async with api_sm() as s:  # 对照：直接查，RLS 只让 bob 看到他自己的 run（一个也没有）
            visible = await s.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.conversation_id == org["conv"]))
    finally:
        principal_var.reset(token)
    assert visible == 0
    assert after["runs"]["queued"] - before["runs"]["queued"] == 1  # 函数以表所有者身份聚合：看到的是全局
    assert after["documents"]["failed"] - before["documents"]["failed"] == 1
    assert after["purge_jobs"]["retrying"] - before["purge_jobs"]["retrying"] == 1
    assert after["purge_jobs"]["max_attempts"] >= 2 and after["runs"]["oldest_queued_seconds"] >= 0
    assert set(after) == {"runs", "documents", "graph_builds", "purge_jobs", "busy_workers"}  # 只有计数，没有行


async def test_admin_status_and_prometheus_endpoint(org, sessionmaker):  # noqa: F811
    app = create_app(with_lifespan=False)
    install_test_auth(app)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    h = lambda who: {"X-User-Id": str(org["users"][who].id)}  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/admin/status", headers=h("bob"))).status_code == 403
        r = await c.get("/api/admin/status", headers=h("admin"))
        assert r.status_code == 200 and "queued" in r.json()["runs"]
        m = await c.get("/metrics")
    assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'agentnexus_runs{status="queued"}' in m.text and "# TYPE agentnexus_busy_workers gauge" in m.text


def test_prometheus_text_skips_missing_quantiles():
    status = {"runs": {"queued": 2, "running": 0, "awaiting_approval": 0, "oldest_queued_seconds": 12.5,
                       "succeeded_24h": 0, "failed_24h": 1, "p50_seconds_24h": None, "p95_seconds_24h": None},
              "documents": {"pending": 0, "processing": 0, "failed": 3, "oldest_pending_seconds": 0},
              "graph_builds": {"pending": 0, "running": 0},
              "purge_jobs": {"pending": 1, "retrying": 1, "max_attempts": 4, "oldest_seconds": 30},
              "busy_workers": 1}
    text = prometheus(status)
    assert 'agentnexus_runs{status="queued"} 2' in text
    assert "agentnexus_run_oldest_queued_seconds 12.5" in text
    assert "quantile" not in text  # 24 小时内没有成功的 run：分位数不输出
    assert 'agentnexus_purge_jobs{state="retrying"} 1' in text
