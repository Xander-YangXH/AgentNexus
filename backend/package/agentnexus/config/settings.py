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
    sweep_interval_seconds: float = 5.0  # 空闲时多久查一次待执行 / 租约过期的任务（有 NOTIFY 时立即唤醒）
    # 任务租约（decisions/0020）：心跳间隔取租约的 1/3，丢一两次心跳不会被误判为死亡；接管最多等一个租约时长
    job_lease_seconds: float = 30.0
    job_heartbeat_seconds: float = 10.0
    run_max_attempts: int = 3  # 一个 run 最多被执行几次（含接管），超过标为 interrupted
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
    orphan_blob_grace_hours: float = 24.0  # 没有记录引用的原件，超过这么久才删（阶段 7.4）
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
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None
    kg_max_chunks_per_build: int = 2000  # 单次构建最多抽取多少个（去重后的）分块，防止误点花掉一大笔钱
    kg_extract_concurrency: int = 4
    # 模型单价（美元 / 百万 token；decisions/0013 的 DeepSeek 高峰价）。用于构建前的费用预估和构建后的实际费用
    llm_price_input: float = 0.3
    llm_price_cached_input: float = 0.006
    llm_price_output: float = 1.2
    knowledge_prefetch: bool = True  # 调用模型前先用用户原话检索一次（decisions/0018 的评测对比）
    knowledge_top_k: int = 4  # knowledge_search 工具每次给模型几个片段：越多越全，但输入 token 越多（每块约 300 token）  # 召回多少个候选交给重排。实测 10 个约 200 字的候选约 1 秒（CPU），耗时近似线性
    # 解析不可信文件（decisions/0025）：PDF / Word 在子进程里解析，超时或超过内存上限就终止、文档标为失败
    parse_timeout_seconds: float = 60.0
    parse_memory_mb: int = 1024
    ingest_concurrency: int = 1  # 同时处理的文档数。向量化是 CPU 密集的，ONNX 自己就会用满多核，并发多了只会互相抢
    # 工具与沙盒（decisions/0021）。没配置的部分就不提供对应工具（依赖它们的技能也不出现）
    sandbox_url: str | None = None  # sandbox 服务地址，例如 http://sandbox:8000
    sandbox_token: str | None = None
    sandbox_exec_timeout: float = 30.0  # 单次 python_exec 的超时（模型不能改）
    mcp_gateway_url: str | None = None  # 例如 http://mcp-gateway:8080/mcp
    skills_dir: str | None = None  # 默认用代码里自带的 backend/skills
    agent_max_tool_rounds: int = 8  # 加载技能后一次回答最多几轮工具调用（未加载技能时仍是检索的 3 轮）
    # 深度调研（decisions/0022）
    research_timeout_seconds: float = 900.0  # 调研 run 单次执行的超时（不含等待审批的时间）
    research_max_subtasks: int = 5
    research_concurrency: int = 3  # 同时调用模型的子 Agent 数
    approval_ttl_hours: float = 24.0  # 等待审批多久后自动取消
    # 登录（decisions/0023）
    session_ttl_hours: float = 168.0  # 会话有效期（7 天）；退出或被禁用时立即失效
    # 登录限流（decisions/0025）：同一账号同一 IP、同一 IP 在窗口内的尝试上限
    login_pair_limit: int = 5
    login_ip_limit: int = 30
    login_window_seconds: int = 900
    # 客户端 IP 取反向代理设置的 X-Real-IP（compose 里为 true：api 只经 Nginx 访问）。直连 api 时必须为 false，否则 IP 可伪造
    trust_proxy_headers: bool = False
    cookie_secure: bool = False  # 只在 HTTPS 下发送 Cookie。本地 http 演示必须为 false，否则浏览器不保存
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
        job_lease_seconds=float(os.environ.get("JOB_LEASE_SECONDS", 30)),
        job_heartbeat_seconds=float(os.environ.get("JOB_HEARTBEAT_SECONDS", 10)),
        run_max_attempts=int(os.environ.get("RUN_MAX_ATTEMPTS", 3)),
        llm_provider=os.environ.get("LLM_PROVIDER", "fake"),
        llm_model=os.environ.get("LLM_MODEL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY") or None,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_thinking=os.environ.get("LLM_THINKING") or None,
        llm_fake_stream_delay=float(os.environ.get("LLM_FAKE_STREAM_DELAY", 0)),
        checkpoint_retention_hours=float(os.environ.get("CHECKPOINT_RETENTION_HOURS", 24)),
        checkpoint_janitor_interval_seconds=float(os.environ.get("CHECKPOINT_JANITOR_INTERVAL_SECONDS", 600)),
        orphan_blob_grace_hours=float(os.environ.get("ORPHAN_BLOB_GRACE_HOURS", 24)),
        blob_root=os.environ.get("BLOB_ROOT", "/data/blobs"),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", 10)),
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_collection=os.environ.get("QDRANT_COLLECTION", "chunks"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        embedding_threads=int(os.environ["EMBEDDING_THREADS"]) if os.environ.get("EMBEDDING_THREADS") else None,
        ingest_concurrency=int(os.environ.get("INGEST_CONCURRENCY", 1)),
        parse_timeout_seconds=float(os.environ.get("PARSE_TIMEOUT_SECONDS", 60)),
        parse_memory_mb=int(os.environ.get("PARSE_MEMORY_MB", 1024)),
        rerank_enabled=os.environ.get("RERANK_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
        rerank_model=os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3-int8"),
        rerank_candidates=int(os.environ.get("RERANK_CANDIDATES", 10)),
        knowledge_top_k=int(os.environ.get("KNOWLEDGE_TOP_K", 4)),
        neo4j_url=os.environ.get("NEO4J_URL", "bolt://localhost:7687"),
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        neo4j_password=os.environ.get("NEO4J_PASSWORD") or None,
        kg_max_chunks_per_build=int(os.environ.get("KG_MAX_CHUNKS_PER_BUILD", 2000)),
        kg_extract_concurrency=int(os.environ.get("KG_EXTRACT_CONCURRENCY", 4)),
        llm_price_input=float(os.environ.get("LLM_PRICE_INPUT", 0.3)),
        llm_price_cached_input=float(os.environ.get("LLM_PRICE_CACHED_INPUT", 0.006)),
        llm_price_output=float(os.environ.get("LLM_PRICE_OUTPUT", 1.2)),
        knowledge_prefetch=os.environ.get("KNOWLEDGE_PREFETCH", "true").lower() not in ("0", "false", "no", "off"),
        sandbox_url=os.environ.get("SANDBOX_URL") or None,
        sandbox_token=os.environ.get("SANDBOX_TOKEN") or None,
        sandbox_exec_timeout=float(os.environ.get("SANDBOX_EXEC_TIMEOUT", 30)),
        mcp_gateway_url=os.environ.get("MCP_GATEWAY_URL") or None,
        skills_dir=os.environ.get("SKILLS_DIR") or None,
        agent_max_tool_rounds=int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", 8)),
        research_timeout_seconds=float(os.environ.get("RESEARCH_TIMEOUT_SECONDS", 900)),
        research_max_subtasks=int(os.environ.get("RESEARCH_MAX_SUBTASKS", 5)),
        research_concurrency=int(os.environ.get("RESEARCH_CONCURRENCY", 3)),
        approval_ttl_hours=float(os.environ.get("APPROVAL_TTL_HOURS", 24)),
        session_ttl_hours=float(os.environ.get("SESSION_TTL_HOURS", 168)),
        cookie_secure=os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes", "on"),
        login_pair_limit=int(os.environ.get("LOGIN_PAIR_LIMIT", 5)),
        login_ip_limit=int(os.environ.get("LOGIN_IP_LIMIT", 30)),
        login_window_seconds=int(os.environ.get("LOGIN_WINDOW_SECONDS", 900)),
        trust_proxy_headers=os.environ.get("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes", "on"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        log_format=os.environ.get("LOG_FORMAT", "text"),
    )
