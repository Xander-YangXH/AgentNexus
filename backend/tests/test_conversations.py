"""对话管理（decisions/0012）：改标题、归档、按最近活动排序、自动标题、消息游标分页。"""

import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.services.runs import RunService, auto_title
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app
from agentnexus.runtime.dispatcher import RunDispatcher


@pytest_asyncio.fixture
async def api(sessionmaker):
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: RunDispatcher(sessionmaker, lambda ctx: None)  # 不执行
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _user(api):
    return {"X-User-Id": (await api.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]}


async def _conv(api, h, title=None):
    return (await api.post("/api/conversations", json={"title": title}, headers=h)).json()


async def _finish_active(sessionmaker, cid):
    """把对话当前活跃的 run 结束掉，以便继续发下一条消息（同一对话最多一个活跃 run）。"""
    from agentnexus.repositories.runs import AgentRunRepository

    async with sessionmaker() as session:
        run = await AgentRunRepository(session).get_active(uuid.UUID(cid))
    svc = RunService(sessionmaker)
    await svc.claim(run.id)
    await svc.complete(run.id, uuid.UUID(cid), "a")


# ---------- 自动标题 ----------

@pytest.mark.parametrize(
    "content, expected",
    [
        ("你好", "你好"),
        ("  第一行  \n第二行", "第一行"),
        ("长" * 31, "长" * 30 + "…"),
        ("长" * 30, "长" * 30),
        ("   \n  ", None),
    ],
)
def test_auto_title(content, expected):
    assert auto_title(content) == expected


async def test_first_message_sets_title_only_when_empty(api):
    h = await _user(api)
    c = await _conv(api, h)
    await api.post(f"/api/conversations/{c['id']}/messages", json={"content": "第一个问题"}, headers=h)
    assert (await api.get("/api/conversations", headers=h)).json()[0]["title"] == "第一个问题"
    named = await _conv(api, h, title="我起的名字")
    await api.post(f"/api/conversations/{named['id']}/messages", json={"content": "别覆盖"}, headers=h)
    titles = {x["id"]: x["title"] for x in (await api.get("/api/conversations", headers=h)).json()}
    assert titles[named["id"]] == "我起的名字"


# ---------- 改标题 / 归档 ----------

async def test_rename_and_clear_title(api):
    h = await _user(api)
    c = await _conv(api, h, title="旧")
    r = await api.patch(f"/api/conversations/{c['id']}", json={"title": "新"}, headers=h)
    assert r.status_code == 200 and r.json()["title"] == "新"
    # 只传 archived，不传 title：标题不能被改成 null
    assert (await api.patch(f"/api/conversations/{c['id']}", json={"archived": False}, headers=h)).json()["title"] == "新"
    # 显式传 null：清空
    assert (await api.patch(f"/api/conversations/{c['id']}", json={"title": None}, headers=h)).json()["title"] is None
    assert (await api.patch(f"/api/conversations/{c['id']}", json={"title": ""}, headers=h)).status_code == 422


async def test_archive_hides_blocks_sending_and_can_be_restored(api, sessionmaker):
    h = await _user(api)
    c = await _conv(api, h)
    await api.post(f"/api/conversations/{c['id']}/messages", json={"content": "q"}, headers=h)
    await _finish_active(sessionmaker, c["id"])

    archived = (await api.patch(f"/api/conversations/{c['id']}", json={"archived": True}, headers=h)).json()
    assert archived["archived_at"] is not None
    first_time = archived["archived_at"]
    again = (await api.patch(f"/api/conversations/{c['id']}", json={"archived": True}, headers=h)).json()
    assert again["archived_at"] == first_time  # 重复归档保留第一次的时间

    assert (await api.get("/api/conversations", headers=h)).json() == []
    assert [x["id"] for x in (await api.get("/api/conversations?archived=true", headers=h)).json()] == [c["id"]]
    assert len((await api.get(f"/api/conversations/{c['id']}/messages", headers=h)).json()) == 2  # 仍可查看

    r = await api.post(f"/api/conversations/{c['id']}/messages", json={"content": "x"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conversation_archived"

    await api.patch(f"/api/conversations/{c['id']}", json={"archived": False}, headers=h)
    assert (await api.post(f"/api/conversations/{c['id']}/messages", json={"content": "y"}, headers=h)).status_code == 202


async def test_retry_is_blocked_in_archived_conversation(api, sessionmaker):
    h = await _user(api)
    c = await _conv(api, h)
    run = (await api.post(f"/api/conversations/{c['id']}/messages", json={"content": "q"}, headers=h)).json()
    await api.post(f"/api/runs/{run['id']}/cancel", headers=h)
    await api.patch(f"/api/conversations/{c['id']}", json={"archived": True}, headers=h)
    r = await api.post(f"/api/runs/{run['id']}/retry", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conversation_archived"


async def test_cannot_touch_other_users_conversations(api):
    alice, bob = await _user(api), await _user(api)
    c = await _conv(api, alice)
    r = await api.patch(f"/api/conversations/{c['id']}", json={"title": "x"}, headers=bob)
    assert r.status_code == 404
    assert (await api.get(f"/api/conversations/{c['id']}/messages?limit=1", headers=bob)).status_code == 404


# ---------- 排序 ----------

async def test_list_is_ordered_by_last_activity(api, sessionmaker):
    h = await _user(api)
    older, newer = await _conv(api, h, "older"), await _conv(api, h, "newer")
    ids = lambda r: [x["id"] for x in r.json()]  # noqa: E731
    assert ids(await api.get("/api/conversations", headers=h)) == [newer["id"], older["id"]]
    await api.post(f"/api/conversations/{older['id']}/messages", json={"content": "活跃一下"}, headers=h)
    assert ids(await api.get("/api/conversations", headers=h)) == [older["id"], newer["id"]]  # 有新消息的排到前面


# ---------- 消息分页 ----------

async def test_cursor_pagination_walks_back_without_gaps_or_duplicates(api, sessionmaker):
    h = await _user(api)
    c = await _conv(api, h)
    for i in range(7):  # 7 轮 = 14 条消息
        await api.post(f"/api/conversations/{c['id']}/messages", json={"content": f"q{i}"}, headers=h)
        await _finish_active(sessionmaker, c["id"])

    url = f"/api/conversations/{c['id']}/messages"
    page = await api.get(f"{url}?limit=5", headers=h)
    assert page.headers["x-has-more"] == "true"
    collected = page.json()
    assert [m["content"] for m in collected][-1] == "a"  # 第一页是最近的 5 条，按时间正序
    while page.headers["x-has-more"] == "true":
        page = await api.get(f"{url}?limit=5&before_seq={collected[0]['seq']}", headers=h)
        collected = page.json() + collected
    seqs = [m["seq"] for m in collected]
    assert len(collected) == 14 and seqs == sorted(seqs) and len(set(seqs)) == 14
    assert [m["content"] for m in collected[::2]] == [f"q{i}" for i in range(7)]


async def test_pagination_params_are_validated(api):
    h = await _user(api)
    c = await _conv(api, h)
    url = f"/api/conversations/{c['id']}/messages"
    assert (await api.get(f"{url}?limit=0", headers=h)).status_code == 422
    assert (await api.get(f"{url}?limit=201", headers=h)).status_code == 422
    assert (await api.get(f"{url}?before_seq=0", headers=h)).status_code == 422
    r = await api.get(url, headers=h)
    assert r.json() == [] and r.headers["x-has-more"] == "false"


async def test_conversation_list_can_use_the_partial_index(sessionmaker):
    """对生产代码实际使用的查询做 EXPLAIN（第一版在测试里另写了一条查询，生产查询改坏了也测不出来）。"""
    from agentnexus.repositories.conversations import conversations_list_stmt

    stmt = conversations_list_stmt(uuid.uuid4(), archived=False)
    async with sessionmaker() as session:
        conn = await session.connection()
        compiled = stmt.compile(dialect=conn.dialect)
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in (await conn.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params)).all())
    assert "ix_conversations_user_recent_active" in plan, plan
