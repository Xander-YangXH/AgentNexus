from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.config import get_settings
from agentnexus.models import build_chat_model
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.errors import (
    ConversationNotFound,
    RunAlreadyFinished,
    RunConflict,
    RunNotFound,
    RunNotRetryable,
    UserExists,
)
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader, make_reader_client
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.storage.checkpoint import open_pool
from agentnexus.storage.postgres import make_engine, make_sessionmaker
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from server.routers import conversations, events, health, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = make_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = make_sessionmaker(engine)
    # checkpoint 表由 migrate 服务建好；这里只负责连接
    checkpoint_pool = await open_pool(settings.database_url)
    agent = ChatbotAgent(
        ChatbotContext(model=build_chat_model(settings)),
        checkpointer=AsyncPostgresSaver(checkpoint_pool),
    )
    # 两个 Redis 客户端：发布与订阅分开。SSE 每个客户端占一个阻塞连接，
    # 如果共用一个连接池，订阅者一多就会把连接占满，导致执行方的事件发不出去
    publish_redis = Redis.from_url(settings.redis_url, decode_responses=True, max_connections=20)
    sse_redis = make_reader_client(
        settings.redis_url,
        block_seconds=settings.sse_poll_interval_seconds,
        max_connections=settings.sse_redis_max_connections,
    )
    app.state.dispatcher = RunDispatcher(
        app.state.sessionmaker,
        agent.run,
        concurrency=settings.worker_concurrency,
        run_timeout=settings.run_timeout_seconds,
        sweep_interval=settings.sweep_interval_seconds,
        events=RunEventPublisher(
            publish_redis, maxlen=settings.event_stream_maxlen, ttl_seconds=settings.event_stream_ttl_seconds
        ),
    )
    app.state.event_streamer = RunEventStreamer(
        app.state.sessionmaker, RunEventReader(sse_redis), poll_interval=settings.sse_poll_interval_seconds
    )
    await app.state.dispatcher.start()
    yield
    await app.state.dispatcher.stop()
    await sse_redis.aclose()
    await publish_redis.aclose()
    await checkpoint_pool.close()
    await engine.dispose()


def _error(status: int, message: str):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": message})

    return handler


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """with_lifespan=False 供测试使用：不连接真实环境，由测试自己往 app.state 里放依赖。"""
    app = FastAPI(title="AgentNexus", lifespan=lifespan if with_lifespan else None)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(conversations.router)
    app.include_router(events.router)
    # 服务层只抛领域异常，HTTP 状态码在这里统一映射
    app.add_exception_handler(ConversationNotFound, _error(404, "conversation not found"))
    app.add_exception_handler(RunNotFound, _error(404, "run not found"))
    app.add_exception_handler(RunConflict, _error(409, "conversation already has an active run"))
    app.add_exception_handler(UserExists, _error(409, "user name already exists"))
    app.add_exception_handler(RunAlreadyFinished, _error(409, "run already finished"))
    app.add_exception_handler(RunNotRetryable, _error(409, "run cannot be retried"))
    return app


app = create_app()
