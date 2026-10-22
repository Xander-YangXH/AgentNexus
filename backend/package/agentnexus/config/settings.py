"""配置统一从环境变量读取（compose 通过 .env 注入），代码里不写死连接串和密钥。"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    worker_concurrency: int = 4  # 同时执行的 run 数上限
    run_timeout_seconds: float = 300.0  # 单个 run 的执行超时
    sweep_interval_seconds: float = 5.0  # 扫描 queued 补投递的间隔
    # LLM：默认 fake（确定性回声模型），不需要密钥即可跑通整条链路
    llm_provider: str = "fake"  # fake | openai（OpenAI 兼容接口） | anthropic | google
    llm_model: str = ""
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # 思考模式：None = 用供应商默认（DeepSeek 默认开启且强度高）；"enabled" / "disabled"。
    # 实测思考 token 按输出计费（回答“1+1”时 30 个输出 token 里 28 个是思考），聊天场景关闭可明显省钱
    llm_thinking: str | None = None
    llm_fake_stream_delay: float = 0.0  # 假模型每块之间的间隔（秒），本地演示流式效果用
    # checkpoint 清理（decisions/0010）：run 结束后保留多久，以及多久检查一次
    checkpoint_retention_hours: float = 24.0
    checkpoint_janitor_interval_seconds: float = 600.0
    # 知识库（阶段 2）：原件存储目录（compose 里挂数据卷）与单个文件大小上限
    blob_root: str = "/data/blobs"
    max_upload_mb: int = 10
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "chunks"  # 别名；实际集合名带时间戳，重建索引时切换（storage/vectors.py）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_threads: int | None = None  # None：用满所有核。设小可给同时进行的检索留 CPU，但入库变慢（见 knowledge/embedding.py）
    rerank_enabled: bool = True  # 关闭则不加载重排模型（省约 0.8GB 内存），检索只用向量召回
    rerank_model: str = "BAAI/bge-reranker-v2-m3-int8"
    rerank_candidates: int = 10
    knowledge_prefetch: bool = True  # 调用模型前先用用户原话检索一次（decisions/0018 的评测对比）
    knowledge_top_k: int = 4  # knowledge_search 工具每次给模型几个片段：越多越全，但输入 token 越多（每块约 300 token）  # 召回多少个候选交给重排。实测 10 个约 200 字的候选约 1 秒（CPU），耗时近似线性
    ingest_concurrency: int = 1  # 同时处理的文档数。向量化是 CPU 密集的，ONNX 自己就会用满多核，并发多了只会互相抢
    log_level: str = "INFO"
    log_format: str = "text"  # text（给人看）| json（给机器看）
    # 流式事件（decisions/0007）。用代码默认值、不开放环境变量，需要时再接入 compose
    event_stream_ttl_seconds: int = 3600  # run 结束后事件流保留多久（供断线重连回放）
    event_stream_maxlen: int = 5000  # 单条事件流的近似最大长度
    sse_poll_interval_seconds: float = 5.0  # SSE 空闲多久查一次数据库
    # 每个 SSE 客户端占一个阻塞的 Redis 连接，这个值约等于单进程能同时服务的流数。
    # redis-py 默认是 100（已查证），这里显式设置并与发布用的客户端分开，避免读者占满连接导致事件发不出去
    sse_redis_max_connections: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        redis_url=os.environ["REDIS_URL"],
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", 4)),
        run_timeout_seconds=float(os.environ.get("RUN_TIMEOUT_SECONDS", 300)),
        sweep_interval_seconds=float(os.environ.get("SWEEP_INTERVAL_SECONDS", 5)),
        llm_provider=os.environ.get("LLM_PROVIDER", "fake"),
        llm_model=os.environ.get("LLM_MODEL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY") or None,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_thinking=os.environ.get("LLM_THINKING") or None,
        llm_fake_stream_delay=float(os.environ.get("LLM_FAKE_STREAM_DELAY", 0)),
        checkpoint_retention_hours=float(os.environ.get("CHECKPOINT_RETENTION_HOURS", 24)),
        checkpoint_janitor_interval_seconds=float(os.environ.get("CHECKPOINT_JANITOR_INTERVAL_SECONDS", 600)),
        blob_root=os.environ.get("BLOB_ROOT", "/data/blobs"),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", 10)),
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_collection=os.environ.get("QDRANT_COLLECTION", "chunks"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        embedding_threads=int(os.environ["EMBEDDING_THREADS"]) if os.environ.get("EMBEDDING_THREADS") else None,
        ingest_concurrency=int(os.environ.get("INGEST_CONCURRENCY", 1)),
        rerank_enabled=os.environ.get("RERANK_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
        rerank_model=os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3-int8"),
        rerank_candidates=int(os.environ.get("RERANK_CANDIDATES", 10)),
        knowledge_top_k=int(os.environ.get("KNOWLEDGE_TOP_K", 4)),
        knowledge_prefetch=os.environ.get("KNOWLEDGE_PREFETCH", "true").lower() not in ("0", "false", "no", "off"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        log_format=os.environ.get("LOG_FORMAT", "text"),
    )
