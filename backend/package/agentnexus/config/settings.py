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
    llm_fake_stream_delay: float = 0.0  # 假模型每块之间的间隔（秒），本地演示流式效果用
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
        llm_fake_stream_delay=float(os.environ.get("LLM_FAKE_STREAM_DELAY", 0)),
    )
