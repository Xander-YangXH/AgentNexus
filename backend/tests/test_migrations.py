"""迁移本身的测试：在一个独立的新库上按版本逐步升级，验证数据迁移的结果。"""

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from agentnexus.storage.postgres.session import to_sqlalchemy_url

DB = "agentnexus_migtest"
HERE = os.path.dirname(__file__)


@pytest.fixture
def fresh_db():
    admin = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not admin:
        pytest.skip("需要 DATABASE_URL")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    url = make_url(admin).set(database=DB).render_as_string(hide_password=False)
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB)))


def _alembic(url: str) -> Config:
    cfg = Config(os.path.join(HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "..", "migrations"))
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_url(url).replace("%", "%%"))
    cfg.set_main_option("path_separator", "os")
    return cfg


def test_0003_backfills_error_code_from_raw_error_text(fresh_db):
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "0002")  # 停在加列之后、回填之前：模拟“历史数据”

    raw_errors = {
        "timeout after 300.0s": "timeout",
        "interrupted: worker restarted": "interrupted",
        "GoogleAPIError: 503 UNAVAILABLE. {'error': {'code': 503}}": "llm_unavailable",  # 开发库里真实出现过的报错
        "RateLimitError: Error code: 429": "llm_rate_limited",
        "GoogleInvalidRequestError: 400 INVALID_ARGUMENT API_KEY_INVALID": "llm_config",
        "ValueError: bad prompt": "internal",
        "RuntimeError: took 5003 ms": "internal",  # 5003 不是状态码：要按“整词”匹配，不能误判成 5xx
    }
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        uid, cid, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO users (id, name) VALUES (%s, 'm')", (uid,))
        conn.execute("INSERT INTO conversations (id, user_id) VALUES (%s, %s)", (cid, uid))
        conn.execute("INSERT INTO messages (id, conversation_id, role, content) VALUES (%s, %s, 'user', 'q')", (mid, cid))
        ids = {}
        for raw in raw_errors:
            rid = uuid.uuid4()
            ids[rid] = raw
            conn.execute(
                "INSERT INTO agent_runs (id, conversation_id, input_message_id, status, error) "
                "VALUES (%s, %s, %s, 'failed', %s)",
                (rid, cid, mid, raw),
            )
        # 已经有错误码的行、非失败的行都不能被改动
        kept, ok = uuid.uuid4(), uuid.uuid4()
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, input_message_id, status, error, error_code) "
            "VALUES (%s, %s, %s, 'failed', 'timeout after 1s', 'llm_unavailable')",
            (kept, cid, mid),
        )
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, input_message_id, status) VALUES (%s, %s, %s, 'cancelled')",
            (ok, cid, mid),
        )

    command.upgrade(cfg, "head")

    with psycopg.connect(fresh_db) as conn:
        got = dict(conn.execute("SELECT id, error_code FROM agent_runs").fetchall())
    assert {ids[r]: got[r] for r in ids} == raw_errors
    assert got[kept] == "llm_unavailable"  # 不覆盖已有值
    assert got[ok] is None  # 非失败行不动

    command.upgrade(cfg, "head")  # 再执行一次不会出错（已在 head，无操作）


def test_0005_backfills_last_activity_from_messages(fresh_db):
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "0004")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        uid, with_msgs, empty = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO users (id, name) VALUES (%s, 'm')", (uid,))
        conn.execute(
            "INSERT INTO conversations (id, user_id, created_at) VALUES (%s, %s, '2026-01-01T00:00:00Z'), (%s, %s, '2026-02-01T00:00:00Z')",
            (with_msgs, uid, empty, uid),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES "
            "(%s, 'user', 'a', '2026-03-01T00:00:00Z'), (%s, 'assistant', 'b', '2026-03-05T00:00:00Z')",
            (with_msgs, with_msgs),
        )
    command.upgrade(cfg, "head")
    with psycopg.connect(fresh_db) as conn:
        got = dict(conn.execute("SELECT id, to_char(last_activity_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') FROM conversations").fetchall())
        archived = conn.execute("SELECT count(*) FROM conversations WHERE archived_at IS NOT NULL").fetchone()[0]
    assert got[with_msgs] == "2026-03-05"  # 最后一条消息的时间
    assert got[empty] == "2026-02-01"  # 没有消息：用创建时间
    assert archived == 0
