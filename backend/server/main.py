from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentnexus.agents.echo import echo_agent
from agentnexus.config import get_settings
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.services.errors import ConversationNotFound, RunConflict, RunNotFound, UserExists
from agentnexus.storage.postgres import make_engine, make_sessionmaker
from server.routers import conversations, health, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = make_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.dispatcher = RunDispatcher(
        app.state.sessionmaker,
        echo_agent,  # 1.4 换成 LangGraph chatbot
        concurrency=settings.worker_concurrency,
        run_timeout=settings.run_timeout_seconds,
        sweep_interval=settings.sweep_interval_seconds,
    )
    await app.state.dispatcher.start()
    yield
    await app.state.dispatcher.stop()
    await engine.dispose()


def _error(status: int, message: str):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": message})

    return handler


def create_app() -> FastAPI:
    app = FastAPI(title="AgentNexus", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(conversations.router)
    # 服务层只抛领域异常，HTTP 状态码在这里统一映射
    app.add_exception_handler(ConversationNotFound, _error(404, "conversation not found"))
    app.add_exception_handler(RunNotFound, _error(404, "run not found"))
    app.add_exception_handler(RunConflict, _error(409, "conversation already has an active run"))
    app.add_exception_handler(UserExists, _error(409, "user name already exists"))
    return app


app = create_app()
