"""装配：api 进程与 worker 进程共用的组件（decisions/0020 起两个进程分开）。

- api：接收请求、落库、发 NOTIFY、读事件流；检索测试页面要用 embedding / 重排模型，所以也加载它们；不执行后台任务；
- worker：执行对话 run、文档入库、图谱构建，以及 checkpoint 清理。
两边都加载模型是这次拆分的主要内存代价（各约 1GB），换来的是向量化不再和接口抢 CPU、worker 可以多副本。
"""

import asyncio
import contextlib
import dataclasses
from datetime import timedelta

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from agentnexus.agents.buildin.chatbot import ChatbotAgent, ChatbotContext
from agentnexus.config import Settings
from agentnexus.events.redis_stream import RunEventPublisher
from agentnexus.knowledge.embedding import FastEmbedder
from agentnexus.knowledge.graph.extraction import FakeGraphExtractor, LLMGraphExtractor
from agentnexus.knowledge.graph.store import Neo4jGraphStore
from agentnexus.knowledge.rerank import FastReranker
from agentnexus.models import build_chat_model
from agentnexus.runtime.cancel_bus import RedisCancelBus
from agentnexus.runtime.dispatcher import RunDispatcher
from agentnexus.runtime.jobs import JobDispatcher
from agentnexus.runtime.janitor import CheckpointJanitor
from agentnexus.services.graph import GraphService, Prices
from agentnexus.services.knowledge import KnowledgeService
from agentnexus.services.search import SearchService
from agentnexus.storage.blobs import LocalBlobStore
from agentnexus.storage.checkpoint import open_pool
from agentnexus.storage.postgres import make_engine, make_sessionmaker
from agentnexus.storage.vectors import ChunkIndex


@dataclasses.dataclass
class Components:
    settings: Settings
    sessionmaker: object
    knowledge_service: KnowledgeService
    search_service: SearchService
    graph_service: GraphService
    run_dispatcher: RunDispatcher
    ingest_dispatcher: JobDispatcher
    graph_dispatcher: JobDispatcher
    cancel_bus: RedisCancelBus
    janitor: CheckpointJanitor | None
    _stack: contextlib.AsyncExitStack


async def build(settings: Settings, *, worker: bool) -> Components:
    stack = contextlib.AsyncExitStack()
    engine = make_engine(settings.database_url, pool_pre_ping=True)
    stack.push_async_callback(engine.dispose)
    sessionmaker = make_sessionmaker(engine)
    publish_redis = Redis.from_url(settings.redis_url, decode_responses=True, max_connections=20)
    stack.push_async_callback(publish_redis.aclose)
    cancel_bus = RedisCancelBus(publish_redis)

    # 加载 embedding 模型要读文件、建 ONNX 会话，放到线程里；模型在构建镜像时已下载，这里不联网
    embedder = await asyncio.to_thread(FastEmbedder, settings.embedding_model, threads=settings.embedding_threads)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=10)
    stack.push_async_callback(qdrant.close)
    index = ChunkIndex(qdrant, settings.qdrant_collection, embedder.dim)
    await index.ensure()  # 维度对不上（换了模型）直接启动失败，比悄悄写坏索引好
    knowledge = KnowledgeService(
        sessionmaker, LocalBlobStore(settings.blob_root), embedder, index,
        max_upload_bytes=settings.max_upload_mb * 1024 * 1024, token_counter=embedder.token_counter(),
        lease_seconds=settings.job_lease_seconds,
    )
    reranker = await asyncio.to_thread(FastReranker, settings.rerank_model) if settings.rerank_enabled else None
    search = SearchService(sessionmaker, embedder, index, reranker, candidates=settings.rerank_candidates)

    # 知识图谱（decisions/0019）。抽取用强制工具调用（结构化输出），与思考模式不兼容，所以单独建一个关闭思考的模型
    neo4j_driver = AsyncGraphDatabase.driver(settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password or ""))
    stack.push_async_callback(neo4j_driver.close)
    graph_store = Neo4jGraphStore(neo4j_driver)
    await graph_store.ensure_schema()
    extractor = (FakeGraphExtractor() if settings.llm_provider == "fake"
                 else LLMGraphExtractor(build_chat_model(dataclasses.replace(settings, llm_thinking="disabled"))))
    graph = GraphService(
        sessionmaker, extractor, graph_store,
        max_chunks_per_build=settings.kg_max_chunks_per_build, concurrency=settings.kg_extract_concurrency,
        prices=Prices(settings.llm_price_input, settings.llm_price_cached_input, settings.llm_price_output),
        lease_seconds=settings.job_lease_seconds,
    )

    handler, janitor = _not_executed_here, None
    if worker:
        # 对话 Agent 与 checkpoint 只在 worker 里需要（checkpoint 表由 migrate 服务建好，这里只负责连接）
        checkpoint_pool = await open_pool(settings.database_url)
        stack.push_async_callback(checkpoint_pool.close)
        checkpointer = AsyncPostgresSaver(checkpoint_pool)

        async def knowledge_search(user_id, kb_ids, query):
            return (await search.search(user_id, kb_ids, query, top_k=settings.knowledge_top_k)).hits

        agent = ChatbotAgent(
            ChatbotContext(model=build_chat_model(settings), knowledge_search=knowledge_search,
                           prefetch=settings.knowledge_prefetch),
            checkpointer=checkpointer,
        )
        handler = agent.run
        janitor = CheckpointJanitor(
            sessionmaker, checkpointer, retention=timedelta(hours=settings.checkpoint_retention_hours),
            interval_seconds=settings.checkpoint_janitor_interval_seconds,
        )

    lease = {"lease_seconds": settings.job_lease_seconds, "heartbeat_seconds": settings.job_heartbeat_seconds}
    runs = RunDispatcher(
        sessionmaker, handler, concurrency=settings.worker_concurrency, run_timeout=settings.run_timeout_seconds,
        sweep_interval=settings.sweep_interval_seconds, max_attempts=settings.run_max_attempts, cancel_bus=cancel_bus,
        events=RunEventPublisher(publish_redis, maxlen=settings.event_stream_maxlen,
                                 ttl_seconds=settings.event_stream_ttl_seconds),
        **lease,
    )
    ingest = JobDispatcher(knowledge, name="ingest", concurrency=settings.ingest_concurrency,
                           sweep_interval=settings.sweep_interval_seconds, heartbeat_seconds=settings.job_heartbeat_seconds)
    # 一次只跑一个构建：每个构建内部已经并发调用模型
    graph_jobs = JobDispatcher(graph, name="graph", concurrency=1, sweep_interval=settings.sweep_interval_seconds,
                               heartbeat_seconds=settings.job_heartbeat_seconds)
    return Components(settings, sessionmaker, knowledge, search, graph, runs, ingest, graph_jobs, cancel_bus, janitor, stack)


async def close(c: Components) -> None:
    await c._stack.aclose()


async def _not_executed_here(ctx):  # api 进程不执行 run（RunDispatcher 在 api 里不会 start）
    raise RuntimeError("runs are executed by the worker process")
