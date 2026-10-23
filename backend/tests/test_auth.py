"""登录、会话、CSRF 与管理员接口（阶段 5，decisions/0023）。这里走真实的 Cookie 会话，不用测试替身。"""

import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from agentnexus.security.auth import AuthService, token_hash, verify_password
from agentnexus.storage.postgres import UserSession
from server.deps import SESSION_COOKIE, get_sessionmaker
from server.main import create_app

PASSWORD = "test-password-123"


@pytest_asyncio.fixture
async def app_env(sessionmaker):
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    auth = AuthService(sessionmaker)
    app.state.auth_service = auth
    admin = await auth.create_user(f"admin-{uuid.uuid4().hex[:6]}", PASSWORD, role="admin")
    member = await auth.create_user(f"m-{uuid.uuid4().hex[:6]}", PASSWORD)
    yield app, auth, admin, member


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(c, name, password=PASSWORD):
    return await c.post("/api/auth/login", json={"name": name, "password": password})


async def test_login_sets_an_httponly_cookie_and_logout_revokes_the_session(app_env, sessionmaker):
    app, _, _, member = app_env
    async with _client(app) as c:
        assert (await c.get("/api/conversations")).status_code == 401
        r = await _login(c, member.name)
        assert r.status_code == 200 and r.json()["name"] == member.name and r.json()["role"] == "member"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
        token = c.cookies[SESSION_COOKIE]
        assert (await c.get("/api/conversations")).status_code == 200
        assert (await c.get("/api/auth/me")).json()["id"] == str(member.id)
        # 库里只有令牌的哈希
        async with sessionmaker() as session:
            stored = (await session.scalars(select(UserSession.token_hash).where(UserSession.user_id == member.id))).all()
        assert stored == [token_hash(token)] and token not in stored

        assert (await c.post("/api/auth/logout")).status_code == 204
    # 退出后，即使有人留着旧 Cookie，会话也已经删了
    async with _client(app) as c2:
        c2.cookies.set(SESSION_COOKIE, token)
        assert (await c2.get("/api/conversations")).status_code == 401


async def test_wrong_password_and_unknown_user_look_the_same(app_env):
    app, _, _, member = app_env
    async with _client(app) as c:
        wrong = await _login(c, member.name, "not-the-password")
        unknown = await _login(c, f"nobody-{uuid.uuid4().hex[:6]}", "whatever-123")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert SESSION_COOKIE not in wrong.headers.get("set-cookie", "")


async def test_user_without_password_cannot_log_in(app_env, user):
    """升级前就存在的用户没有密码：不能用空密码或任何密码登录。"""
    app, *_ = app_env
    async with _client(app) as c:
        assert (await _login(c, user.name, "")).status_code == 422
        assert (await _login(c, user.name, "anything-123")).status_code == 401
    assert not await verify_password(None, "anything-123")


async def test_disabling_or_resetting_password_revokes_existing_sessions(app_env):
    app, auth, admin, member = app_env
    async with _client(app) as m, _client(app) as a:
        await _login(m, member.name)
        await _login(a, admin.name)
        assert (await m.get("/api/conversations")).status_code == 200
        r = await a.patch(f"/api/admin/users/{member.id}", json={"disabled": True})
        assert r.status_code == 200 and r.json()["disabled_at"] is not None
        assert (await m.get("/api/conversations")).status_code == 401  # 即刻生效
        assert (await _login(m, member.name)).status_code == 401
        await a.patch(f"/api/admin/users/{member.id}", json={"disabled": False, "password": "new-password-456"})
        assert (await _login(m, member.name)).status_code == 401
        assert (await _login(m, member.name, "new-password-456")).status_code == 200


async def test_disabled_user_is_rejected_even_if_a_session_survived(app_env, sessionmaker):
    """禁用时会删掉会话；会话校验时再检查一次 disabled_at（例如直接在库里禁用、或删除会话失败时）。"""
    from sqlalchemy import update

    from agentnexus.storage.postgres import User

    app, _, _, member = app_env
    async with _client(app) as m:
        await _login(m, member.name)
        async with sessionmaker() as s, s.begin():
            await s.execute(update(User).where(User.id == member.id).values(disabled_at=func.now()))
        assert (await m.get("/api/conversations")).status_code == 401


async def test_password_reset_alone_revokes_sessions(app_env):
    app, auth, admin, member = app_env
    async with _client(app) as m:
        await _login(m, member.name)
        await auth.update_user(member.id, password="another-password-789")
        assert (await m.get("/api/conversations")).status_code == 401


async def test_dummy_hash_used_for_timing_never_logs_anyone_in(app_env, user):
    """用户不存在 / 没有密码时，会拿一个固定的假哈希做一次校验（让响应时间一样）——它对应的明文绝不能变成万能密码。"""
    app, *_ = app_env
    async with _client(app) as c:
        assert (await _login(c, user.name, "dummy-password-for-timing")).status_code == 401
        assert (await _login(c, f"nobody-{uuid.uuid4().hex[:4]}", "dummy-password-for-timing")).status_code == 401


async def test_expired_session_is_rejected(sessionmaker, app_env):
    app, _, _, member = app_env
    short = AuthService(sessionmaker, session_ttl=timedelta(seconds=-1))
    result = await short.login(member.name, PASSWORD)
    async with _client(app) as c:
        c.cookies.set(SESSION_COOKIE, result.token)
        assert (await c.get("/api/conversations")).status_code == 401


async def test_admin_endpoints_require_admin(app_env):
    app, _, admin, member = app_env
    async with _client(app) as m, _client(app) as a:
        await _login(m, member.name)
        await _login(a, admin.name)
        assert (await m.get("/api/admin/users")).status_code == 403
        assert (await m.post("/api/admin/departments", json={"name": "x"})).status_code == 403
        dept = (await a.post("/api/admin/departments", json={"name": f"研发-{uuid.uuid4().hex[:4]}"})).json()
        created = await a.post("/api/admin/users", json={"name": f"new-{uuid.uuid4().hex[:6]}", "password": "pw-12345678",
                                                         "role": "manager", "department_id": dept["id"]})
        assert created.status_code == 201 and created.json()["role"] == "manager"
        assert (await a.post("/api/admin/users", json={"name": "short", "password": "1234567"})).status_code == 422
        assert (await a.post("/api/admin/users", json={"name": created.json()["name"], "password": "pw-12345678"})).status_code == 409
        r = await a.patch(f"/api/admin/users/{member.id}", json={"department_id": dept["id"]})
        assert r.json()["department_id"] == dept["id"]
        assert (await a.patch(f"/api/admin/users/{member.id}", json={"department_id": None})).json()["department_id"] is None
        assert any(u["id"] == str(member.id) for u in (await a.get("/api/admin/users")).json())
        assert "password_hash" not in (await a.get("/api/admin/users")).text


@pytest.mark.parametrize("headers, expected", [
    ({}, 201),  # 非浏览器客户端（没有 Origin）：不是 CSRF 的攻击面
    ({"Origin": "http://test"}, 201),  # 同源
    ({"Origin": "https://evil.example"}, 403),
    ({"Origin": "http://test:8080"}, 403),  # 端口不同也是不同的源
    ({"Origin": "http://test", "Sec-Fetch-Site": "cross-site"}, 403),
])
async def test_csrf_rejects_cross_site_writes(app_env, headers, expected):
    app, _, _, member = app_env
    async with _client(app) as c:
        await _login(c, member.name)
        r = await c.post("/api/conversations", json={}, headers=headers)
        assert r.status_code == expected
        if expected == 403:
            assert r.json()["error"]["code"] == "csrf_rejected"
        assert (await c.get("/api/conversations", headers=headers)).status_code == 200  # 读请求不受影响
