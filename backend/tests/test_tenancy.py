"""部门隔离与越权矩阵（阶段 5，decisions/0023）。

同一组场景跑两遍：
- owner：用迁移 / 表所有者角色连接（RLS 不生效），只测应用层的检查；
- api：用生产里 api 进程的普通角色连接（RLS 生效），既确认第二道防线不误伤正常操作，也确认越权仍被拒绝。
另有“漏写过滤条件”的直接查询测试：只有 RLS 能挡住。
"""

import uuid

import httpx
import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from agentnexus.security.principal import Principal, install_rls_identity, principal_var
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.usage import UsageService, usage_insert
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.postgres import (
    Conversation,
    Department,
    Document,
    KnowledgeBase,
    Message,
    UsageEvent,
    UsageKind,
    User,
    make_engine,
    make_sessionmaker,
)
from agentnexus.storage.roles import ensure_roles
from agentnexus.agents.echo import echo_agent
from agentnexus.runtime.dispatcher import RunDispatcher
from server.deps import get_dispatcher, get_ingest_dispatcher, get_sessionmaker, get_usage_service
from server.main import create_app
from tests.fakes import FakeEmbedder, install_test_auth

API_ROLE, WORKER_ROLE = "agentnexus_test_api", "agentnexus_test_worker"
ROLE_PASSWORD = "test-role-password"


@pytest.fixture(scope="session")
def role_urls(test_db_url):
    ensure_roles(test_db_url, api_role=API_ROLE, api_password=ROLE_PASSWORD,
                 worker_role=WORKER_ROLE, worker_password=ROLE_PASSWORD)
    url = make_url(test_db_url)
    as_role = lambda r: url.set(username=r, password=ROLE_PASSWORD).render_as_string(hide_password=False)
    return {"api": as_role(API_ROLE), "worker": as_role(WORKER_ROLE)}


@pytest_asyncio.fixture
async def role_sessionmaker(role_urls):
    engines = []

    def make(role):
        engine = make_engine(role_urls[role])
        engines.append(engine)
        return make_sessionmaker(engine)

    install_rls_identity()
    yield make
    for e in engines:
        await e.dispose()


@pytest_asyncio.fixture
async def org(sessionmaker):
    """两个部门：A（alice、bob、经理 mgr）、B（carol），一个管理员。alice 有一个部门共享、一个私有的知识库和一个对话。"""
    tag = uuid.uuid4().hex[:6]
    async with sessionmaker() as s, s.begin():
        a, b = Department(name=f"A-{tag}"), Department(name=f"B-{tag}")
        s.add_all([a, b])
        await s.flush()
        users = {n: User(name=f"{n}-{tag}", role=r, department_id=d.id if d else None)
                 for n, r, d in [("alice", "member", a), ("bob", "member", a), ("mgr", "manager", a),
                                 ("carol", "member", b), ("admin", "admin", None)]}
        s.add_all(users.values())
        await s.flush()
        shared = KnowledgeBase(user_id=users["alice"].id, name="共享库", visibility="department", department_id=a.id)
        private = KnowledgeBase(user_id=users["alice"].id, name="私有库")
        conv = Conversation(user_id=users["alice"].id, title="alice 的对话")
        s.add_all([shared, private, conv])
        await s.flush()
        s.add(Message(conversation_id=conv.id, role="user", content="alice 的秘密问题"))
        for n in ("alice", "bob", "carol"):
            await s.execute(usage_insert(users[n].id, UsageKind.LLM, "chat", input_tokens=100, output_tokens=10))
    return {"users": users, "dept_a": a.id, "dept_b": b.id, "shared": shared.id, "private": private.id, "conv": conv.id}


@pytest_asyncio.fixture(params=["owner", "api"])
async def api(request, sessionmaker, role_sessionmaker, org, tmp_path, chunk_index):
    sm = sessionmaker if request.param == "owner" else role_sessionmaker("api")
    app = create_app(with_lifespan=False)
    install_test_auth(app)
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_dispatcher] = lambda: RunDispatcher(sm, echo_agent)
    app.dependency_overrides[get_ingest_dispatcher] = lambda: type("D", (), {"enqueue": lambda self, _: True})()
    app.dependency_overrides[get_usage_service] = lambda: UsageService(sm, price_input=0.3, price_cached=0.006, price_output=1.2)
    app.state.knowledge_service = KnowledgeService(sm, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, org, request.param


def _h(org, name):
    return {"X-User-Id": str(org["users"][name].id)}


async def test_knowledge_base_visibility_matrix(api):
    c, org, _ = api
    alice, bob, carol = _h(org, "alice"), _h(org, "bob"), _h(org, "carol")
    shared, private = org["shared"], org["private"]
    up = await c.post(f"/api/knowledge-bases/{shared}/documents", files={"file": ("a.txt", "退货政策".encode())}, headers=alice)
    assert up.status_code == 202, up.text
    doc = up.json()["id"]

    # 列表：alice 两个都有；bob 只有共享的；carol 都没有
    lists = {n: sorted(k["name"] for k in (await c.get("/api/knowledge-bases", headers=h)).json())
             for n, h in [("alice", alice), ("bob", bob), ("carol", carol)]}
    assert lists == {"alice": sorted(["私有库", "共享库"]), "bob": ["共享库"], "carol": []}

    cases = [  # (who, method, path, expected)
        (bob, "GET", f"/api/knowledge-bases/{shared}", 200),
        (bob, "GET", f"/api/knowledge-bases/{shared}/documents", 200),
        (bob, "GET", f"/api/documents/{doc}", 200),
        (bob, "GET", f"/api/documents/{doc}/chunks", 200),
        (bob, "GET", f"/api/knowledge-bases/{private}", 404),
        (carol, "GET", f"/api/knowledge-bases/{shared}", 404),
        (carol, "GET", f"/api/documents/{doc}", 404),
        (bob, "PATCH", f"/api/knowledge-bases/{shared}", 403),  # 看得见、但只读
        (carol, "PATCH", f"/api/knowledge-bases/{shared}", 404),
        (bob, "POST", f"/api/documents/{doc}/reprocess", 403),
    ]
    for who, method, path, expected in cases:
        body = {"json": {"visibility": "private"}} if method == "PATCH" else {}
        r = await c.request(method, path, headers=who, **body)
        assert r.status_code == expected, (method, path, r.status_code, r.text)
    r = await c.post(f"/api/knowledge-bases/{shared}/documents", files={"file": ("b.txt", b"x")}, headers=bob)
    assert r.status_code == 403
    assert (await c.get(f"/api/knowledge-bases/{shared}")).status_code == 401


async def test_sharing_and_unsharing(api):
    c, org, _ = api
    alice, bob = _h(org, "alice"), _h(org, "bob")
    private = org["private"]
    assert (await c.patch(f"/api/knowledge-bases/{private}", json={"visibility": "department"}, headers=alice)).json()["department_id"] == str(org["dept_a"])
    assert (await c.get(f"/api/knowledge-bases/{private}", headers=bob)).status_code == 200
    await c.patch(f"/api/knowledge-bases/{private}", json={"visibility": "private"}, headers=alice)
    assert (await c.get(f"/api/knowledge-bases/{private}", headers=bob)).status_code == 404
    admin = _h(org, "admin")  # 没有部门的用户不能共享
    kb = (await c.post("/api/knowledge-bases", json={"name": "管理员的库"}, headers=admin)).json()["id"]
    r = await c.patch(f"/api/knowledge-bases/{kb}", json={"visibility": "department"}, headers=admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_department"


async def test_conversations_stay_private_but_can_use_shared_knowledge(api):
    c, org, _ = api
    bob, carol = _h(org, "bob"), _h(org, "carol")
    conv = org["conv"]
    for path in (f"/api/conversations/{conv}/messages", f"/api/conversations/{conv}/runs",
                 f"/api/conversations/{conv}/knowledge-bases"):
        assert (await c.get(path, headers=bob)).status_code == 404, path
    assert (await c.post(f"/api/conversations/{conv}/messages", json={"content": "hi"}, headers=bob)).status_code == 404
    mine = (await c.post("/api/conversations", json={}, headers=bob)).json()["id"]
    r = await c.put(f"/api/conversations/{mine}/knowledge-bases", json={"knowledge_base_ids": [str(org["shared"])]}, headers=bob)
    assert r.status_code == 200 and r.json()["knowledge_base_ids"] == [str(org["shared"])]
    r = await c.put(f"/api/conversations/{mine}/knowledge-bases", json={"knowledge_base_ids": [str(org["private"])]}, headers=bob)
    assert r.status_code == 404
    other = (await c.post("/api/conversations", json={}, headers=carol)).json()["id"]
    r = await c.put(f"/api/conversations/{other}/knowledge-bases", json={"knowledge_base_ids": [str(org["shared"])]}, headers=carol)
    assert r.status_code == 404
    # 正常使用：bob 在自己的对话里发消息
    assert (await c.post(f"/api/conversations/{mine}/messages", json={"content": "你好"}, headers=bob)).status_code == 202


async def test_usage_scopes_by_role(api):
    c, org, _ = api
    get = lambda who, **q: c.get("/api/usage/summary", params=q, headers=_h(org, who))
    assert (await get("bob")).json()["totals"]["llm_calls"] == 1
    assert (await get("bob", scope="department")).status_code == 403
    dept = (await get("mgr", scope="department")).json()
    assert dept["totals"]["llm_calls"] == 2 and {u["name"].split("-")[0] for u in dept["by_user"]} == {"alice", "bob"}
    assert (await get("mgr", scope="all")).status_code == 403
    assert (await get("mgr", scope="department", department_id=str(org["dept_b"]))).status_code == 403
    assert (await get("admin", scope="department", department_id=str(org["dept_b"]))).json()["totals"]["llm_calls"] == 1
    assert (await get("admin", scope="all")).json()["totals"]["llm_calls"] >= 3


# ---------- 只有 RLS 能挡住的：漏写过滤条件 ----------

async def test_rls_filters_queries_that_forgot_the_where_clause(org, role_sessionmaker):
    api_sm, worker_sm = role_sessionmaker("api"), role_sessionmaker("worker")
    u = org["users"]

    async def count(sm, model, who=None):
        token = principal_var.set(Principal(u[who].id, u[who].name, u[who].role, u[who].department_id) if who else None)
        try:
            async with sm() as s:
                return await s.scalar(select(func.count()).select_from(model).where(
                    model.id.in_([org["shared"], org["private"]]) if model is KnowledgeBase else
                    model.id == org["conv"] if model is Conversation else text("true")))
        finally:
            principal_var.reset(token)

    assert await count(api_sm, KnowledgeBase, "alice") == 2
    assert await count(api_sm, KnowledgeBase, "bob") == 1  # 共享的那个
    assert await count(api_sm, KnowledgeBase, "carol") == 0
    assert await count(api_sm, KnowledgeBase) == 0  # 没设置身份：默认拒绝
    assert await count(api_sm, Conversation, "bob") == 0
    assert await count(api_sm, Conversation, "alice") == 1
    # 子表跟着父表走：bob 看不到 alice 对话里的任何消息
    token = principal_var.set(Principal(u["bob"].id, u["bob"].name, "member", u["bob"].department_id))
    try:
        async with api_sm() as s:
            leaked = await s.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == org["conv"]))
            mine_usage = await s.scalar(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.user_id.in_([x.id for x in u.values()])))
    finally:
        principal_var.reset(token)
    assert leaked == 0 and mine_usage == 1
    # worker 角色 BYPASSRLS：后台任务按 run 归属取数据，不受影响
    assert await count(worker_sm, KnowledgeBase) == 2


async def test_rls_rejects_writes_as_someone_else(org, role_sessionmaker):
    api_sm = role_sessionmaker("api")
    u = org["users"]
    token = principal_var.set(Principal(u["bob"].id, u["bob"].name, "member", u["bob"].department_id))
    try:
        with pytest.raises(Exception, match="row-level security"):
            async with api_sm() as s, s.begin():
                s.add(KnowledgeBase(user_id=u["alice"].id, name=f"冒名-{uuid.uuid4().hex[:4]}"))
        with pytest.raises(Exception, match="row-level security"):
            async with api_sm() as s, s.begin():
                s.add(Document(id=uuid.uuid4(), knowledge_base_id=org["shared"], filename="x.txt", size_bytes=1, sha256="0" * 64,
                               blob_key="documents/x", status="pending", chunk_count=0))  # 共享库对 bob 只读
    finally:
        principal_var.reset(token)


def test_session_level_identity_does_not_leak_to_the_next_transaction(role_urls, org):
    """实验 2 的陷阱 c：用事务级设置，连接被复用时下一个事务看不到上一个用户的数据。"""
    alice = org["users"]["alice"]
    with psycopg.connect(role_urls["api"]) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(alice.id),))
            assert conn.execute("SELECT count(*) FROM conversations WHERE id = %s", (org["conv"],)).fetchone()[0] == 1
        with conn.transaction():
            assert conn.execute("SELECT count(*) FROM conversations WHERE id = %s", (org["conv"],)).fetchone()[0] == 0


async def test_identity_hook_does_not_leak_through_the_connection_pool(org, role_urls):
    """应用里的钩子（security/principal.py）用的是事务级设置：同一条池化连接上，下一个没有身份的事务什么都看不到。"""
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    engine = make_engine(role_urls["api"], poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0)
    sm = make_sessionmaker(engine)
    install_rls_identity()
    alice = org["users"]["alice"]
    try:
        token = principal_var.set(Principal(alice.id, alice.name, "member", alice.department_id))
        # 必须是提交的事务：回滚会连“会话级”的设置一起撤销，只读的测试测不出泄漏（第一版就是只读的，变异检查发现它测不出问题）
        async with sm() as s, s.begin():
            pid_a = await s.scalar(text("select pg_backend_pid()"))
            assert await s.scalar(select(func.count()).select_from(Conversation).where(Conversation.id == org["conv"])) == 1
        principal_var.reset(token)
        async with sm() as s:  # 池里只有一条连接：一定是同一条
            assert await s.scalar(text("select pg_backend_pid()")) == pid_a
            assert await s.scalar(select(func.count()).select_from(Conversation).where(Conversation.id == org["conv"])) == 0
    finally:
        await engine.dispose()


async def test_unshared_knowledge_base_is_dropped_at_execution_time(sessionmaker, org):
    """bob 关联了共享库之后，alice 取消共享：bob 的下一次执行不能再检索它（执行时按可见性重新过滤）。"""
    from agentnexus.repositories.conversations import ConversationRepository
    from agentnexus.services.runs import RunService

    bob = org["users"]["bob"]
    async with sessionmaker() as s, s.begin():
        conv = await ConversationRepository(s).create(bob.id)
        await ConversationRepository(s).set_knowledge_bases(conv.id, [org["shared"]])
    svc = RunService(sessionmaker)
    run = await svc.submit(bob.id, conv.id, "q")
    assert (await svc.load_context(run.id)).knowledge_base_ids == (org["shared"],)
    async with sessionmaker() as s, s.begin():
        (await s.get(KnowledgeBase, org["shared"])).visibility = "private"
    assert (await svc.load_context(run.id)).knowledge_base_ids == ()

