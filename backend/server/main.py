import asyncio
import dataclasses
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.config import get_settings
from agentnexus.models import build_chat_model
from datetime import timedelta

from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.runtime.janitor import CheckpointJanitor
from agentnexus.events.redis_stream import RunEventPublisher, RunEventReader, make_reader_client
from agentnexus.services.run_events import RunEventStreamer
from agentnexus.knowledge.embedding import FastEmbedder
from agentnexus.knowledge.graph.extraction import FakeGraphExtractor, LLMGraphExtractor
from agentnexus.knowledge.graph.store import Neo4jGraphStore
from agentnexus.knowledge.rerank import FastReranker
from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.services.graph import GraphService, Prices
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.checkpoint import open_pool
from agentnexus.storage.vectors import ChunkIndex
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from agentnexus.storage.postgres import make_engine, make_sessionmaker
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from agentnexus.observability.logging import configure_logging
from server.errors import install_error_handlers
from server.middleware import RequestContextMiddleware
from server.routers import conversations, events, graph, health, knowledge, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = make_sessionmaker(engine)
    # checkpoint 表由 migrate 服务建好；这里只负责连接
    checkpoint_pool = await open_pool(settings.database_url)
    checkpointer = AsyncPostgresSaver(checkpoint_pool)
    janitor = CheckpointJanitor(
        app.state.sessionmaker,
        checkpointer,
        retention=timedelta(hours=settings.checkpoint_retention_hours),
        interval_seconds=settings.checkpoint_janitor_interval_seconds,
    )
    # 两个 Redis 客户端：发布与订阅分开。SSE 每个客户端占一个阻塞连接，
    # 如果共用一个连接池，订阅者一多就会把连接占满，导致执行方的事件发不出去
    publish_redis = Redis.from_url(settings.redis_url, decode_responses=True, max_connections=20)
    sse_redis = make_reader_client(
        settings.redis_url,
        block_seconds=settings.sse_poll_interval_seconds,
        max_connections=settings.sse_redis_max_connections,
    )
    # 加载 embedding 模型要读文件、建 ONNX 会话，放到线程里；模型在构建镜像时已下载，这里不联网
    embedder = await asyncio.to_thread(FastEmbedder, settings.embedding_model, threads=settings.embedding_threads)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=10)
    index = ChunkIndex(qdrant, settings.qdrant_collection, embedder.dim)
    await index.ensure()  # 维度对不上（换了模型）直接启动失败，比悄悄写坏索引好
    app.state.knowledge_service = KnowledgeService(
        app.state.sessionmaker,
        LocalBlobStore(settings.blob_root),
        embedder,
        index,
        max_upload_bytes=settings.max_upload_mb * 1024 * 1024,
        token_counter=embedder.token_counter(),
    )
    reranker = await asyncio.to_thread(FastReranker, settings.rerank_model) if settings.rerank_enabled else None
    app.state.search_service = SearchService(
        app.state.sessionmaker, embedder, index, reranker, candidates=settings.rerank_candidates
    )
    search_service = app.state.search_service

    async def knowledge_search(user_id, kb_ids, query):
        return (await search_service.search(user_id, kb_ids, query, top_k=settings.knowledge_top_k)).hits

    agent = ChatbotAgent(
        ChatbotContext(
            model=build_chat_model(settings), knowledge_search=knowledge_search, prefetch=settings.knowledge_prefetch
        ),
        checkpointer=checkpointer,
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
    app.state.ingest_dispatcher = JobDispatcher(
        app.state.knowledge_service,
        name="ingest",
        concurrency=settings.ingest_concurrency,
        sweep_interval=settings.sweep_interval_seconds,
    )
    # 知识图谱（decisions/0019）。抽取用强制工具调用（结构化输出），与思考模式不兼容，所以单独建一个关闭思考的模型
    neo4j_driver = AsyncGraphDatabase.driver(settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password or ""))
    graph_store = Neo4jGraphStore(neo4j_driver)
    await graph_store.ensure_schema()
    extractor = (FakeGraphExtractor() if settings.llm_provider == "fake"
                 else LLMGraphExtractor(build_chat_model(dataclasses.replace(settings, llm_thinking="disabled"))))
    app.state.graph_service = GraphService(
        app.state.sessionmaker, extractor, graph_store,
        max_chunks_per_build=settings.kg_max_chunks_per_build, concurrency=settings.kg_extract_concurrency,
        prices=Prices(settings.llm_price_input, settings.llm_price_cached_input, settings.llm_price_output),
    )
    # 一次只跑一个构建：每个构建内部已经并发调用模型
    app.state.graph_dispatcher = JobDispatcher(
        app.state.graph_service, name="graph", concurrency=1, sweep_interval=settings.sweep_interval_seconds
    )
    await app.state.dispatcher.start()
    await app.state.ingest_dispatcher.start()
    await app.state.graph_dispatcher.start()
    janitor.start()
    yield
    await janitor.stop()
    await app.state.graph_dispatcher.stop()
    await app.state.ingest_dispatcher.stop()
    await app.state.dispatcher.stop()
    await qdrant.close()
    await neo4j_driver.close()
    await sse_redis.aclose()
    await publish_redis.aclose()
    await checkpoint_pool.close()
    await engine.dispose()


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """with_lifespan=False 供测试使用：不连接真实环境，由测试自己往 app.state 里放依赖。"""
    app = FastAPI(title="AgentNexus", lifespan=lifespan if with_lifespan else None)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(conversations.router)
    app.include_router(events.router)
    app.include_router(knowledge.router)
    app.include_router(graph.router)
    # 服务层只抛领域异常，HTTP 状态码与统一错误格式在这里映射（server/errors.py）
    install_error_handlers(app)
    # 请求上下文：request-id + 访问日志。纯 ASGI 中间件，不影响 SSE 流式
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_app()
