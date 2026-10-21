"""知识库（阶段 2.1）：文件存储、上传、解析分块、归属。"""

import uuid

import httpx
import pytest
import pytest_asyncio

from agentnexus.services.knowledge import KnowledgeService, clean_filename
from agentnexus.storage.blobs import BlobNotFound, LocalBlobStore
from server.deps import get_ingest_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import FakeEmbedder

KB_TEXT = "退货政策：签收后 7 天内可以无理由退货。定制商品不支持退货。\n\n退款到账：入库检验后 1 到 3 个工作日原路退回。"


# ---------- 文件存储 ----------

async def test_blob_store_roundtrip_and_atomic_write(tmp_path):
    store = LocalBlobStore(tmp_path)
    await store.put("documents/abc", b"hello")
    assert await store.get("documents/abc") == b"hello"
    assert not [p for p in tmp_path.rglob(".tmp-*")]  # 原子写入：不留临时文件
    await store.delete("documents/abc")
    with pytest.raises(BlobNotFound):
        await store.get("documents/abc")
    await store.delete("documents/abc")  # 删除不存在的也不报错


@pytest.mark.parametrize("key", ["../etc/passwd", "documents/../../x", "/abs", "UPPER", "a" * 300, ""])
async def test_blob_store_rejects_unsafe_keys(tmp_path, key):
    with pytest.raises(ValueError):
        await LocalBlobStore(tmp_path).put(key, b"x")


@pytest.mark.parametrize(
    "raw, expected",
    [("../../etc/passwd.txt", "passwd.txt"), ("C:\\Users\\me\\报告.docx", "报告.docx"), ("  ", "unnamed"), ("a" * 300 + ".txt", ("a" * 300 + ".txt")[:255])],
)
def test_clean_filename(raw, expected):
    assert clean_filename(raw) == expected


# ---------- 接口与处理 ----------

@pytest_asyncio.fixture
async def kb_api(sessionmaker, tmp_path, chunk_index):
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    svc = KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index, max_upload_bytes=2048)
    app.state.knowledge_service = svc
    # 这里的测试手动调用 svc.process，所以上传后的投递用一个什么都不做的替身（后台派发器见 test_ingest.py）
    app.dependency_overrides[get_ingest_dispatcher] = lambda: _NoopDispatcher()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, svc, tmp_path


async def _user(c):
    return {"X-User-Id": (await c.post("/api/users", json={"name": f"u-{uuid.uuid4().hex[:8]}"})).json()["id"]}


async def _kb(c, h, name="客服知识库"):
    r = await c.post("/api/knowledge-bases", json={"name": name}, headers=h)
    assert r.status_code == 201
    return r.json()["id"]


class _NoopDispatcher:
    def enqueue(self, document_id):
        return True


def _file(name, data):
    return {"file": (name, data, "application/octet-stream")}


async def test_upload_process_and_read_chunks(kb_api):
    c, svc, _ = kb_api
    h = await _user(c)
    kb = await _kb(c, h)
    r = await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("../../政策.txt", KB_TEXT.encode()), headers=h)
    assert r.status_code == 202
    doc = r.json()
    assert (doc["status"], doc["filename"], doc["chunk_count"]) == ("pending", "政策.txt", 0)

    assert await svc.process(uuid.UUID(doc["id"]))
    assert not await svc.process(uuid.UUID(doc["id"]))  # 同一文档只处理一次

    got = (await c.get(f"/api/documents/{doc['id']}", headers=h)).json()
    assert got["status"] == "ready" and got["chunk_count"] >= 1 and got["processed_at"]
    chunks = (await c.get(f"/api/documents/{doc['id']}/chunks", headers=h)).json()
    assert "".join(ch["text"] for ch in chunks).count("退货政策") >= 1
    assert [d["id"] for d in (await c.get(f"/api/knowledge-bases/{kb}/documents", headers=h)).json()] == [doc["id"]]


async def test_duplicate_file_is_rejected_and_its_blob_is_cleaned_up(kb_api):
    c, _, root = kb_api
    h = await _user(c)
    kb = await _kb(c, h)
    first = (await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("a.txt", b"same content"), headers=h)).json()
    r = await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("b.txt", b"same content"), headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["details"] == {"existing_document_id": first["id"]}
    assert sorted(p.name for p in (root / "documents").iterdir()) == [first["id"]]  # 被拒的那次没有留下孤儿文件
    # 同样的内容放进另一个知识库是允许的
    kb2 = await _kb(c, h, "另一个库")
    assert (await c.post(f"/api/knowledge-bases/{kb2}/documents", files=_file("a.txt", b"same content"), headers=h)).status_code == 202


@pytest.mark.parametrize(
    "name, data, status, code",
    [
        ("a.exe", b"MZ", 415, "unsupported_file_type"),
        ("a.txt", b"", 400, "empty_file"),
        ("a.txt", b"x" * 2049, 413, "file_too_large"),
    ],
)
async def test_upload_validation(kb_api, name, data, status, code):
    c, _, root = kb_api
    h = await _user(c)
    kb = await _kb(c, h)
    r = await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file(name, data), headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert not (root / "documents").exists()  # 校验不通过时什么也不写


async def test_corrupt_file_fails_with_code(kb_api):
    c, svc, _ = kb_api
    h = await _user(c)
    kb = await _kb(c, h)
    doc = (await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("broken.pdf", b"not a pdf at all"), headers=h)).json()
    await svc.process(uuid.UUID(doc["id"]))
    got = (await c.get(f"/api/documents/{doc['id']}", headers=h)).json()
    assert (got["status"], got["error_code"], got["chunk_count"]) == ("failed", "corrupt", 0)
    assert "error" not in got  # 原始报错不对外
    assert (await c.get(f"/api/documents/{doc['id']}/chunks", headers=h)).json() == []


async def test_knowledge_base_name_unique_per_user_and_ownership(kb_api):
    c, _, root = kb_api
    alice, bob = await _user(c), await _user(c)
    kb = await _kb(c, alice)
    assert (await c.post("/api/knowledge-bases", json={"name": "客服知识库"}, headers=alice)).json()["error"]["code"] == "knowledge_base_exists"
    assert (await c.post("/api/knowledge-bases", json={"name": "客服知识库"}, headers=bob)).status_code == 201  # 不同用户可以同名
    doc = (await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("a.txt", "内容".encode()), headers=alice)).json()
    for url in (f"/api/knowledge-bases/{kb}", f"/api/knowledge-bases/{kb}/documents", f"/api/documents/{doc['id']}", f"/api/documents/{doc['id']}/chunks"):
        assert (await c.get(url, headers=bob)).status_code == 404, url
    r = await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("b.txt", "别人的".encode()), headers=bob)
    assert r.status_code == 404
    assert len(list((root / "documents").iterdir())) == 1  # 无权限的上传没有写下任何文件（归属校验在写文件之前）
    assert [k["name"] for k in (await c.get("/api/knowledge-bases", headers=bob)).json()] == ["客服知识库"]  # 只看到自己的


async def test_chunks_are_rolled_back_if_the_document_cannot_be_marked_ready(kb_api, monkeypatch):
    """“写入分块”和“标记完成”在同一事务：标记失败（例如状态被别处改掉）时分块也不能留下。"""
    from agentnexus.repositories.knowledge import ChunkRepository, DocumentRepository
    from agentnexus.storage.postgres import DocumentStatus

    c, svc, _ = kb_api
    h = await _user(c)
    kb = await _kb(c, h)
    doc = (await c.post(f"/api/knowledge-bases/{kb}/documents", files=_file("a.txt", KB_TEXT.encode()), headers=h)).json()
    original = DocumentRepository.finish

    async def refuse_ready(self, document_id, status, **kw):
        if status == DocumentStatus.READY:
            return False
        return await original(self, document_id, status, **kw)

    monkeypatch.setattr(DocumentRepository, "finish", refuse_ready)
    assert await svc.process(uuid.UUID(doc["id"])) is False
    async with svc.sessionmaker() as session:
        assert await ChunkRepository(session).list_by_document(uuid.UUID(doc["id"])) == []


async def test_service_validates_size_before_writing_even_without_the_http_guard(sessionmaker, user, tmp_path, chunk_index):
    """接口层会先挡住超大文件；服务层自己的检查是纵深防御（例如以后 worker 直接调用服务），也必须在写文件之前。"""
    from agentnexus.services.errors import FileTooLarge

    svc = KnowledgeService(sessionmaker, LocalBlobStore(tmp_path), FakeEmbedder(), chunk_index, max_upload_bytes=10)
    kb = await svc.create_kb(user.id, "kb", None)
    with pytest.raises(FileTooLarge):
        await svc.upload(user.id, kb.id, "a.txt", b"x" * 11)
    assert not (tmp_path / "documents").exists()
