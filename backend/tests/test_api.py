import uuid

import httpx
import pytest_asyncio

from agentnexus.agents.echo import echo_agent
from agentnexus.runtime.dispatcher import RunDispatcher
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import install_test_auth


@pytest_asyncio.fixture
async def client(sessionmaker):
    app = create_app()
    install_test_auth(app)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    # 不 start()：只入队、没人消费，run 保持 queued，便于断言“落库后”的状态
    dispatcher = RunDispatcher(sessionmaker, echo_agent)
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _make_user(client, name=None) -> dict:
    r = await client.post("/api/users", json={"name": name or f"u-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 201
    return {"X-User-Id": r.json()["id"]}


async def _make_conv(client, headers) -> str:
    r = await client.post("/api/conversations", json={"title": "t"}, headers=headers)
    assert r.status_code == 201
    return r.json()["id"]


async def test_duplicate_user_name_is_409(client):
    await client.post("/api/users", json={"name": "alice"})
    r = await client.post("/api/users", json={"name": "alice"})
    assert r.status_code == 409


async def test_requires_valid_user_header(client):
    assert (await client.get("/api/conversations")).status_code == 401
    assert (await client.get("/api/conversations", headers={"X-User-Id": "nope"})).status_code == 401
    r = await client.get("/api/conversations", headers={"X-User-Id": str(uuid.uuid4())})
    assert r.status_code == 401


async def test_send_message_returns_202_with_queued_run_and_persists_history(client):
    h = await _make_user(client)
    cid = await _make_conv(client, h)

    r = await client.post(f"/api/conversations/{cid}/messages", json={"content": "你好"}, headers=h)
    assert r.status_code == 202
    run = r.json()
    assert run["status"] == "queued"

    msgs = (await client.get(f"/api/conversations/{cid}/messages", headers=h)).json()
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "你好")]
    assert msgs[0]["id"] == run["input_message_id"]

    got = (await client.get(f"/api/runs/{run['id']}", headers=h)).json()
    assert got["status"] == "queued"


async def test_second_message_while_run_active_is_409(client):
    h = await _make_user(client)
    cid = await _make_conv(client, h)
    await client.post(f"/api/conversations/{cid}/messages", json={"content": "a"}, headers=h)
    r = await client.post(f"/api/conversations/{cid}/messages", json={"content": "b"}, headers=h)
    assert r.status_code == 409
    msgs = (await client.get(f"/api/conversations/{cid}/messages", headers=h)).json()
    assert len(msgs) == 1


async def test_users_cannot_see_each_others_data(client):
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    cid = await _make_conv(client, alice)
    run = (
        await client.post(f"/api/conversations/{cid}/messages", json={"content": "secret"}, headers=alice)
    ).json()

    assert (await client.get("/api/conversations", headers=bob)).json() == []
    # 他人的对话/run 与不存在的表现一致：404，不泄露存在性
    assert (await client.get(f"/api/conversations/{cid}/messages", headers=bob)).status_code == 404
    assert (
        await client.post(f"/api/conversations/{cid}/messages", json={"content": "x"}, headers=bob)
    ).status_code == 404
    assert (await client.get(f"/api/runs/{run['id']}", headers=bob)).status_code == 404
    assert (await client.get(f"/api/runs/{uuid.uuid4()}", headers=alice)).status_code == 404


async def test_validation(client):
    h = await _make_user(client)
    cid = await _make_conv(client, h)
    assert (
        await client.post(f"/api/conversations/{cid}/messages", json={"content": ""}, headers=h)
    ).status_code == 422
    assert (await client.get("/api/conversations/not-a-uuid/messages", headers=h)).status_code == 422


async def test_active_run_lifecycle_and_ownership(client, sessionmaker):
    h = await _make_user(client)
    other = await _make_user(client)
    cid = await _make_conv(client, h)
    assert (await client.get(f"/api/conversations/{cid}/active-run", headers=h)).json() is None

    run = (await client.post(f"/api/conversations/{cid}/messages", json={"content": "q"}, headers=h)).json()
    active = (await client.get(f"/api/conversations/{cid}/active-run", headers=h)).json()
    assert active["id"] == run["id"] and active["status"] == "queued"

    # 别人的对话：与不存在一样 404
    assert (await client.get(f"/api/conversations/{cid}/active-run", headers=other)).status_code == 404

    # run 结束后不再是活跃的
    from agentnexus.services.runs import RunService

    svc = RunService(sessionmaker)
    await svc.claim(uuid.UUID(run["id"]))
    await svc.fail(uuid.UUID(run["id"]), "x")
    assert (await client.get(f"/api/conversations/{cid}/active-run", headers=h)).json() is None
