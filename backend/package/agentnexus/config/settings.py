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
    )
