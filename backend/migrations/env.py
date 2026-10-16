import os

from alembic import context
from sqlalchemy import create_engine

from agentnexus.storage.postgres import Base
from agentnexus.storage.postgres.session import to_sqlalchemy_url

target_metadata = Base.metadata


def _url() -> str:
    # 允许调用方（如测试）通过 config 覆盖，否则读环境变量
    return context.config.get_main_option("sqlalchemy.url") or to_sqlalchemy_url(
        os.environ["DATABASE_URL"]
    )


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
