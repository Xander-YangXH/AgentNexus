"""知识图谱（阶段 3，decisions/0019）：抽取清洗、合并、构建（缓存 / 失败 / 重试）、Neo4j 同步、浏览接口与权限。"""

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from agentnexus.knowledge.graph.extraction import (
    MAX_ENTITIES,
    Entity,
    ExtractResult,
    Extraction,
    FakeGraphExtractor,
    Relation,
    _clean,
    entity_key,
    estimate_tokens,
)
from agentnexus.knowledge.graph.merge import merge
from agentnexus.services.errors import GraphBuildInProgress, GraphBuildTooLarge, KnowledgeBaseEmpty
from agentnexus.services.graph import GraphService, Prices
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.postgres import GraphBuild, GraphBuildStatus, GraphEntity, GraphExtraction, GraphMention, GraphRelation
from server.deps import get_graph_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import FakeEmbedder

# ---------- 纯函数 ----------


def test_clean_drops_dangling_self_loops_and_duplicates():
    ext = Extraction(
        entities=[Entity(name="Qdrant", type="技术组件", description="a"), Entity(name="qdrant ", type="技术组件", description="b"),
                  Entity(name="PostgreSQL", type="技术组件", description="c"), Entity(name="  ", type="概念术语", description="d")],
        relations=[Relation(source="Qdrant", target="PostgreSQL", relation="依赖", description="x"),
                   Relation(source="Qdrant", target="不存在", relation="依赖", description="悬空"),
                   Relation(source="Qdrant", target="qdrant", relation="属于", description="自环")],
    )
    out = _clean(ext)
    assert [e["name"] for e in out["entities"]] == ["Qdrant", "PostgreSQL"]
    assert [(r["source"], r["target"]) for r in out["relations"]] == [("Qdrant", "PostgreSQL")]


def test_extraction_caps_entities_instead_of_failing():
    ext = Extraction(entities=[Entity(name=f"e{i}", type="概念术语", description="") for i in range(20)], relations=[])
    assert len(ext.entities) == MAX_ENTITIES


def test_merge_normalizes_names_votes_types_and_keeps_provenance():
    c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p1 = {"entities": [{"name": "Qdrant", "type": "技术组件", "description": "向量库"},
                       {"name": "PostgreSQL", "type": "技术组件", "description": "数据库"}],
          "relations": [{"source": "Qdrant", "target": "PostgreSQL", "relation": "依赖", "description": "派生"}]}
    p2 = {"entities": [{"name": "qdrant", "type": "概念术语", "description": "向量检索引擎，派生索引"},
                       {"name": "Ｐｏｓｔｇｒｅｓ ＱＬ", "type": "技术组件", "description": "x"}],
          "relations": [{"source": "qdrant", "target": "Ｐｏｓｔｇｒｅｓ ＱＬ", "relation": "依赖", "description": "更长的描述在这里"}]}
    # p2 排在最前：“取第一次出现”会得到 qdrant / 概念术语，多数投票才得到 Qdrant / 技术组件
    entities, relations = merge([(c3, p2), (c1, p1), (c2, p1)])  # p1 被两个内容相同的分块共用
    by_key = {e.key: e for e in entities}
    assert set(by_key) == {"qdrant", "postgresql"}  # 全角、空白、大小写都归一
    q = by_key["qdrant"]
    assert (q.name, q.type, q.description, q.chunk_ids) == ("Qdrant", "技术组件", "向量检索引擎，派生索引", [c3, c1, c2])
    assert len(relations) == 1 and relations[0].chunk_ids == [c3, c1, c2] and relations[0].description == "更长的描述在这里"
    assert (relations[0].source_id, relations[0].target_id) == (q.id, by_key["postgresql"].id)


def test_estimate_and_fake_extractor_are_deterministic():
    assert estimate_tokens([100, 300]) == (2 * 700 + 400, int(2 * 300 + 0.8 * 400))
    assert Prices().cost(1_000_000, 0, 0) == pytest.approx(0.3)
    assert entity_key("Ｒｅｄｉｓ Streams") == "redisstreams"


# ---------- 构建（PostgreSQL + Neo4j） ----------


class RecordingExtractor:
    """可控的抽取器：记录调用；fail_on 里的文本片段触发失败。"""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on
        self._fake = FakeGraphExtractor()

    async def extract(self, title, text):
        self.calls.append(text)
        for marker, exc in self.fail_on:
            if marker in text:
                raise exc
        payload = (await self._fake.extract(title, text)).payload
        return ExtractResult(payload=payload, input_tokens=1000, cached_tokens=400, output_tokens=200)


@pytest_asyncio.fixture
async def graph_store():
    if not os.environ.get("NEO4J_URL"):
        pytest.skip("需要 NEO4J_URL 指向可连接的 Neo4j（docker compose up -d neo4j）")
    from neo4j import AsyncGraphDatabase

    from agentnexus.knowledge.graph.store import Neo4jGraphStore

    driver = AsyncGraphDatabase.driver(os.environ["NEO4J_URL"], auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "")))
    store = Neo4jGraphStore(driver)
    await store.ensure_schema()
    store.created_kbs = []
    yield store
    for kb in store.created_kbs:  # 测试之间不共享数据：删掉本测试建的子图
        await store.delete_kb(kb)
    await driver.close()


@pytest_asyncio.fixture
async def env(sessionmaker, tmp_path, chunk_index, user, graph_store):
    knowledge = KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index)
    kb = await knowledge.create_kb(user.id, "图谱", None)
    graph_store.created_kbs.append(kb.id)
    return knowledge, kb, graph_store


TEXT_A = "## 向量检索\nQdrant 是派生索引，数据以 PostgreSQL 为准。重建索引时切换别名。\n## 事件\nRedis 保存事件流，SSE 断线续传。"
TEXT_B = "## 存储\nPostgreSQL 保存对话与消息。Qdrant 只保存向量。"


async def _add(knowledge, user, kb, name, text):
    doc = await knowledge.upload(user.id, kb.id, name, text.encode())
    assert await knowledge.process(doc.id)
    return doc


def _svc(sessionmaker, store, extractor, **kw):
    return GraphService(sessionmaker, extractor, store, concurrency=2, **kw)


async def _count(session, model, kb_id):
    return await session.scalar(select(func.count()).select_from(model).where(model.knowledge_base_id == kb_id))


async def test_build_extracts_unique_chunks_once_and_syncs_neo4j(env, sessionmaker, user):
    knowledge, kb, store = env
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    # 内容重复的长文本：切出来的块大多完全相同，只应该抽一次
    await _add(knowledge, user, kb, "repeat.txt", "Qdrant 与 PostgreSQL 配合使用。" * 400)
    ext = RecordingExtractor()
    svc = _svc(sessionmaker, store, ext)

    est = await svc.estimate(user.id, kb.id)
    assert est["unique_count"] < est["chunk_count"] and est["cached_count"] == 0
    assert est["extract_count"] == est["unique_count"] and est["estimated_cost_usd"] > 0

    build = await svc.create_build(user.id, kb.id)
    assert build.status == GraphBuildStatus.PENDING
    assert await svc.process(build.id)
    assert not await svc.process(build.id)  # 同一构建只执行一次
    assert len(ext.calls) == est["unique_count"]

    async with sessionmaker() as s:
        done = await s.get(GraphBuild, build.id)
        n_ent, n_rel = await _count(s, GraphEntity, kb.id), await _count(s, GraphRelation, kb.id)
        n_mention = await _count(s, GraphMention, kb.id)
    assert done.status == GraphBuildStatus.SUCCEEDED and done.finished_at
    assert (done.entity_count, done.relation_count) == (n_ent, n_rel) and n_ent > 0 and n_rel > 0
    assert done.done_count == est["unique_count"] and done.input_tokens == 1000 * est["unique_count"]
    assert done.cost_usd == pytest.approx(Prices().cost(done.input_tokens, done.cached_tokens, done.output_tokens), abs=1e-6)  # 存的是保留 6 位小数的值
    assert n_mention >= n_ent  # 每个实体至少一处出处；重复块让“Qdrant”有多处出处
    assert await store.count(kb.id) == (n_ent, n_rel)  # Neo4j 与 PostgreSQL 一致

    # 文档没变时再构建：全部命中缓存，不再调用模型；图被整体替换（id 换新），不残留旧节点
    assert (await svc.estimate(user.id, kb.id))["extract_count"] == 0
    second = await svc.create_build(user.id, kb.id)
    await svc.process(second.id)
    assert len(ext.calls) == est["unique_count"]
    async with sessionmaker() as s:
        assert (await s.get(GraphBuild, second.id)).cost_usd == 0
    assert await store.count(kb.id) == (n_ent, n_rel)


async def test_failed_extraction_keeps_finished_work_and_retry_only_extracts_the_rest(env, sessionmaker, user):
    knowledge, kb, store = env
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    await _add(knowledge, user, kb, "b.md", TEXT_B)

    class RateLimited(Exception):
        status_code = 429

    broken = RecordingExtractor(fail_on=[("Redis", RateLimited("slow down"))])
    svc = _svc(sessionmaker, store, broken)
    build = await svc.create_build(user.id, kb.id)
    await svc.process(build.id)
    async with sessionmaker() as s:
        failed = await s.get(GraphBuild, build.id)
        cached = await _count(s, GraphExtraction, kb.id)
    assert (failed.status, failed.error_code) == (GraphBuildStatus.FAILED, "llm_rate_limited")
    assert "error" not in GraphBuildOut_fields()  # 原始报错不对外（见接口 schema）
    assert cached == failed.done_count  # 成功的部分已写进缓存

    fixed = RecordingExtractor()
    svc = _svc(sessionmaker, store, fixed)
    retry = await svc.create_build(user.id, kb.id)  # 失败的构建不占“进行中”的名额
    await svc.process(retry.id)
    total = (await svc.estimate(user.id, kb.id))["unique_count"]
    assert len(fixed.calls) == total - cached  # 只补没抽过的
    async with sessionmaker() as s:
        assert (await s.get(GraphBuild, retry.id)).status == GraphBuildStatus.SUCCEEDED


def GraphBuildOut_fields():
    from server.schemas import GraphBuildOut

    return GraphBuildOut.model_fields


async def test_neo4j_failure_marks_build_failed_and_resync_needs_no_llm(env, sessionmaker, user, monkeypatch):
    from neo4j.exceptions import ServiceUnavailable

    from agentnexus.knowledge.graph.store import Neo4jGraphStore

    knowledge, kb, store = env
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    ext = RecordingExtractor()
    svc = _svc(sessionmaker, store, ext)
    original = Neo4jGraphStore.replace_kb

    async def down(self, *a, **kw):
        raise ServiceUnavailable("neo4j down")

    monkeypatch.setattr(Neo4jGraphStore, "replace_kb", down)
    build = await svc.create_build(user.id, kb.id)
    await svc.process(build.id)
    async with sessionmaker() as s:
        assert (await s.get(GraphBuild, build.id)).error_code == "graph_store_unavailable"
    calls = len(ext.calls)

    monkeypatch.setattr(Neo4jGraphStore, "replace_kb", original)
    retry = await svc.create_build(user.id, kb.id)
    await svc.process(retry.id)
    assert len(ext.calls) == calls  # 重试只重新同步
    async with sessionmaker() as s:
        assert await store.count(kb.id) == (await _count(s, GraphEntity, kb.id), await _count(s, GraphRelation, kb.id))


async def test_limits_single_active_build_and_lease_takeover(env, sessionmaker, user):
    knowledge, kb, store = env
    with pytest.raises(KnowledgeBaseEmpty):
        await _svc(sessionmaker, store, RecordingExtractor()).create_build(user.id, kb.id)
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    with pytest.raises(GraphBuildTooLarge) as too_large:
        await _svc(sessionmaker, store, RecordingExtractor(), max_chunks_per_build=1).create_build(user.id, kb.id)
    assert too_large.value.limit == 1

    svc = _svc(sessionmaker, store, RecordingExtractor())
    build = await svc.create_build(user.id, kb.id)
    with pytest.raises(GraphBuildInProgress):
        await svc.create_build(user.id, kb.id)
    old = await svc.queue.claim(build.id, "crashed")  # 执行到一半进程被杀
    assert await svc.queue.claim_next("other") is None  # 租约还有效
    async with sessionmaker() as s, s.begin():
        await s.execute(update(GraphBuild).where(GraphBuild.id == build.id)
                        .values(lease_expires_at=func.now() - text("interval '1 second'")))
    taken = await svc.queue.claim_next("new")
    assert taken.version == old.version + 1 and await svc.execute(taken)
    assert await svc.execute(old) is False  # 旧 worker 恢复后：租约已丢，不覆盖接管者写好的图


# ---------- 浏览接口与权限 ----------


@pytest_asyncio.fixture
async def api(env, sessionmaker, user):
    knowledge, kb, store = env
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    await _add(knowledge, user, kb, "b.md", TEXT_B)
    svc = _svc(sessionmaker, store, RecordingExtractor())

    class Jobs:  # 测试里同步执行，代替后台派发器
        async def run(self, build_id):
            await svc.process(build_id)

        def enqueue(self, build_id):
            self.last = build_id
            return True

    jobs = Jobs()
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_graph_dispatcher] = lambda: jobs
    app.state.graph_service = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, kb, jobs


async def test_build_and_browse_through_the_api(api, user):
    c, kb, jobs = api
    h = {"X-User-Id": str(user.id)}
    base = f"/api/knowledge-bases/{kb.id}/graph"
    est = (await c.get(f"{base}/estimate", headers=h)).json()
    assert est["extract_count"] == est["unique_count"] > 0
    r = await c.post(f"{base}/builds", headers=h)
    assert r.status_code == 202 and r.json()["status"] == "pending" and "error" not in r.json()
    assert (await c.post(f"{base}/builds", headers=h)).json()["error"]["code"] == "graph_build_in_progress"
    await jobs.run(jobs.last)
    builds = (await c.get(f"{base}/builds", headers=h)).json()
    assert builds[0]["status"] == "succeeded" and builds[0]["entity_count"] > 0

    overview = (await c.get(f"{base}/overview", headers=h)).json()
    names = {n["name"]: n for n in overview["nodes"]}
    assert "Qdrant" in names and names["Qdrant"]["mentions"] == 2  # 两份文档都提到
    assert overview["edges"]
    found = (await c.get(f"{base}/search", params={"q": "qdr"}, headers=h)).json()
    assert [n["name"] for n in found] == ["Qdrant"]
    qid = names["Qdrant"]["id"]

    detail = (await c.get(f"/api/graph/entities/{qid}", headers=h)).json()
    assert detail["mention_count"] == 2 and {m["filename"] for m in detail["mentions"]} == {"a.md", "b.md"}
    assert all("Qdrant" in m["text"] for m in detail["mentions"])  # 出处是原文
    assert detail["relations"] and {r["direction"] for r in detail["relations"]} <= {"in", "out"}
    nb = (await c.get(f"/api/graph/entities/{qid}/neighbors", headers=h)).json()
    assert qid in {n["id"] for n in nb["nodes"]} and all(qid in (e["source"], e["target"]) for e in nb["edges"])

    other = next(n["id"] for n in overview["nodes"] if n["id"] != qid)
    path = (await c.get(f"{base}/path", params={"source": qid, "target": other}, headers=h)).json()
    assert path is None or (path["nodes"][0]["id"], path["nodes"][-1]["id"]) == (qid, other)


async def test_graph_endpoints_check_ownership(api, user):
    c, kb, jobs = api
    h = {"X-User-Id": str(user.id)}
    await c.post(f"/api/knowledge-bases/{kb.id}/graph/builds", headers=h)
    await jobs.run(jobs.last)
    qid = (await c.get(f"/api/knowledge-bases/{kb.id}/graph/overview", headers=h)).json()["nodes"][0]["id"]
    bob = {"X-User-Id": (await c.post("/api/users", json={"name": f"bob-{uuid.uuid4().hex[:6]}"})).json()["id"]}
    for url in (f"/api/knowledge-bases/{kb.id}/graph/estimate", f"/api/knowledge-bases/{kb.id}/graph/overview",
                f"/api/knowledge-bases/{kb.id}/graph/builds", f"/api/knowledge-bases/{kb.id}/graph/search?q=Q"):
        assert (await c.get(url, headers=bob)).status_code == 404, url
    for url in (f"/api/graph/entities/{qid}", f"/api/graph/entities/{qid}/neighbors"):
        r = await c.get(url, headers=bob)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "entity_not_found"), url
    assert (await c.post(f"/api/knowledge-bases/{kb.id}/graph/builds", headers=bob)).status_code == 404


async def test_neo4j_can_be_rebuilt_from_postgres(env, sessionmaker, user):
    """Neo4j 是派生存储：清空后用 sync 命令从 PostgreSQL 重写，不调用模型。"""
    from agentnexus.knowledge.graph.sync import sync_all

    knowledge, kb, store = env
    await _add(knowledge, user, kb, "a.md", TEXT_A)
    ext = RecordingExtractor()
    svc = _svc(sessionmaker, store, ext)
    build = await svc.create_build(user.id, kb.id)
    await svc.process(build.id)
    before = await store.count(kb.id)
    overview = await store.overview(kb.id)
    await store.delete_kb(kb.id)
    assert await store.count(kb.id) == (0, 0)

    calls = len(ext.calls)
    result = await sync_all(sessionmaker, store)
    assert result[kb.id] == before and await store.count(kb.id) == before and len(ext.calls) == calls
    after = await store.overview(kb.id)
    assert sorted((n["id"], n["mentions"]) for n in after["nodes"]) == sorted((n["id"], n["mentions"]) for n in overview["nodes"])
