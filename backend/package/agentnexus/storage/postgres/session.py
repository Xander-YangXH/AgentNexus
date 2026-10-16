from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def to_sqlalchemy_url(url: str) -> str:
    """应用统一用 postgresql://（psycopg 直连、LangGraph 也用它），SQLAlchemy 需要显式指定驱动。"""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def make_engine(url: str, **kwargs) -> AsyncEngine:
    return create_async_engine(to_sqlalchemy_url(url), **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False：提交后对象仍可读，避免异步下访问属性触发隐式 IO
    return async_sessionmaker(engine, expire_on_commit=False)
