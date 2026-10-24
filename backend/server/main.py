from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from agentnexus import assembly
from agentnexus.config import get_settings
from agentnexus.events.redis_stream import RunEventReader, make_reader_client
from agentnexus.observability.logging import configure_logging
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.security.auth import AuthService
from agentnexus.security.principal import install_rls_identity
from agentnexus.security.throttle import LoginThrottle
from server.csrf import CsrfMiddleware
from server.errors import install_error_handlers
from server.middleware import RequestContextMiddleware
from server.routers import artifacts, conversations, events, graph, health, knowledge, usage, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    """api 进程：接收请求、落库、发通知、读事件流；不执行后台任务（由 worker 进程执行，decisions/0020）。"""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    c = await assembly.build(settings, worker=False)
    app.state.sessionmaker = c.sessionmaker
    app.state.knowledge_service = c.knowledge_service
    app.state.artifact_service = c.artifact_service
    app.state.usage_service = c.usage_service
    app.state.auth_service = AuthService(c.sessionmaker, session_ttl=timedelta(hours=settings.session_ttl_hours))
    app.state.cookie_secure = settings.cookie_secure
    app.state.trust_proxy_headers = settings.trust_proxy_headers
    app.state.login_throttle = LoginThrottle.from_url(
        settings.redis_url, pair_limit=settings.login_pair_limit, ip_limit=settings.login_ip_limit,
        window_seconds=settings.login_window_seconds,
    )
    app.state.search_service = c.search_service
    app.state.graph_service = c.graph_service
    # 这三个在 api 里不 start：enqueue 只是空操作（通知已在提交任务的事务里发出），cancel 写数据库并广播给 worker
    app.state.dispatcher = c.run_dispatcher
    app.state.ingest_dispatcher = c.ingest_dispatcher
    app.state.graph_dispatcher = c.graph_dispatcher
    # SSE 读者用单独的 Redis 连接池：每个 SSE 客户端占一个阻塞连接，订阅者一多会占满连接，影响别的 Redis 操作
    sse_redis = make_reader_client(
        settings.redis_url,
        block_seconds=settings.sse_poll_interval_seconds,
        max_connections=settings.sse_redis_max_connections,
    )
    app.state.event_streamer = RunEventStreamer(
        c.sessionmaker, RunEventReader(sse_redis), poll_interval=settings.sse_poll_interval_seconds
    )
    yield
    await sse_redis.aclose()
    await app.state.login_throttle.close()
    await assembly.close(c)


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """with_lifespan=False 供测试使用：不连接真实环境，由测试自己往 app.state 里放依赖。"""
    app = FastAPI(title="AgentNexus", lifespan=lifespan if with_lifespan else None)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(conversations.router)
    app.include_router(events.router)
    app.include_router(knowledge.router)
    app.include_router(graph.router)
    app.include_router(artifacts.router)
    app.include_router(usage.router)
    # 服务层只抛领域异常，HTTP 状态码与统一错误格式在这里映射（server/errors.py）
    install_error_handlers(app)
    # 每个数据库事务开始时设置当前用户（RLS，decisions/0023）
    install_rls_identity()
    app.state.cookie_secure = False  # 生产 lifespan 里按配置覆盖
    app.state.trust_proxy_headers = False
    app.add_middleware(CsrfMiddleware)
    # 请求上下文：request-id + 访问日志。纯 ASGI 中间件，不影响 SSE 流式（最后添加 = 最外层：CSRF 拒绝的请求也有日志）
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_app()
