"""运行状态（阶段 7.8，decisions/0031）：队列积压、失败、清理重试、执行中的 worker。

数据来自迁移 0014 的 system_status()：业务表就是任务表，直接聚合；函数以表所有者身份执行（绕过 RLS），
只返回计数和时长。给两处用：管理员页面（JSON）和 Prometheus 抓取（/metrics，文本格式）。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


async def system_status(sessionmaker: async_sessionmaker) -> dict:
    async with sessionmaker() as session:
        return (await session.execute(text("SELECT system_status()"))).scalar_one()


def prometheus(status: dict) -> str:
    """Prometheus 文本格式（0.0.4）。不引入客户端库：指标都是抓取时现算的 gauge，没有进程内的计数器要维护。"""
    runs, docs, builds, purge = status["runs"], status["documents"], status["graph_builds"], status["purge_jobs"]
    metrics = [
        ("agentnexus_runs", "Runs by status (active ones)",
         [({"status": s}, runs[s]) for s in ("queued", "running", "awaiting_approval")]),
        ("agentnexus_run_oldest_queued_seconds", "Age of the oldest queued run", [({}, runs["oldest_queued_seconds"])]),
        ("agentnexus_runs_finished_24h", "Runs finished in the last 24 hours",
         [({"outcome": "succeeded"}, runs["succeeded_24h"]), ({"outcome": "failed"}, runs["failed_24h"])]),
        ("agentnexus_run_duration_seconds_24h", "Duration of succeeded runs in the last 24 hours",
         [({"quantile": "0.5"}, runs["p50_seconds_24h"]), ({"quantile": "0.95"}, runs["p95_seconds_24h"])]),
        ("agentnexus_documents", "Documents by status (not ready)",
         [({"status": s}, docs[s]) for s in ("pending", "processing", "failed")]),
        ("agentnexus_document_oldest_pending_seconds", "Age of the oldest pending document",
         [({}, docs["oldest_pending_seconds"])]),
        ("agentnexus_graph_builds", "Graph builds in progress", [({"status": s}, builds[s]) for s in ("pending", "running")]),
        ("agentnexus_purge_jobs", "Cleanup jobs after deletions",
         [({"state": "pending"}, purge["pending"]), ({"state": "retrying"}, purge["retrying"])]),
        ("agentnexus_purge_job_max_attempts", "Most attempts of a pending cleanup job", [({}, purge["max_attempts"])]),
        ("agentnexus_busy_workers", "Worker processes holding a lease right now", [({}, status["busy_workers"])]),
    ]
    lines = []
    for name, help_text, samples in metrics:
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for labels, value in samples:
            if value is None:  # 例如 24 小时内没有成功的 run：分位数不存在，不输出这个样本
                continue
            label = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
            lines.append(f"{name}{label} {float(value):g}")
    return "\n".join(lines) + "\n"
