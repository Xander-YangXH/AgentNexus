"""集成测试跑在真实 Postgres 上（部分唯一索引、条件更新的行为 SQLite 模拟不了）。

用法：docker compose up -d postgres 后，设置 TEST_DATABASE_URL 指向任意库的连接串
（只用来连上去创建 agentnexus_test），或沿用 DATABASE_URL。
"""
import os
import uuid

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from agentnexus.storage.postgres import User, make_engine, make_sessionmaker
from agentnexus.storage.postgres.session import to_sqlalchemy_url

TEST_DB = "agentnexus_test"


def _admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL 或 DATABASE_URL 指向可连接的 Postgres")
    return url


def _test_url(admin_url: str) -> str:
    # 保持 URL 形式，只替换库名
    return make_url(admin_url).set(database=TEST_DB).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(TEST_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    url = _test_url(admin_url)

    # 用真实迁移建表：顺带验证迁移脚本本身是可用的
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_url(url).replace("%", "%%"))
    cfg.set_main_option("path_separator", "os")
    command.upgrade(cfg, "head")

    # LangGraph 的 checkpoint 表由它自己的 setup() 建（生产里由 migrate 服务执行）
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
    return url


@pytest_asyncio.fixture
async def sessionmaker(test_db_url):
    engine = make_engine(test_db_url)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE agent_runs, messages, conversations, users RESTART IDENTITY CASCADE"
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def checkpointer(test_db_url):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agentnexus.storage.checkpoint import open_pool

    pool = await open_pool(test_db_url, max_size=3)
    yield AsyncPostgresSaver(pool)
    async with pool.connection() as conn:  # 与业务表一样，测试间清理，避免互相污染
        await conn.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
    await pool.close()


@pytest_asyncio.fixture
async def user(sessionmaker) -> User:
    async with sessionmaker() as session, session.begin():
        u = User(name=f"u-{uuid.uuid4().hex[:8]}")
        session.add(u)
    return u
