"""worker 心跳与运行状态里的“在线 worker”（阶段 7.8，decisions/0031 补充）

租约只能看到正在执行任务的 worker；空闲的、已经死掉的都看不到。worker 每 10 秒写一行心跳，
system_status() 把 30 秒内有心跳的算作在线（workers_alive）。函数其余部分与 0014 相同。

Revision ID: 0015
Revises: 0014
"""
import runpy
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION system_status() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
SELECT jsonb_build_object(
  'runs', (SELECT jsonb_build_object(
      'queued', count(*) FILTER (WHERE status = 'queued'),
      'running', count(*) FILTER (WHERE status = 'running'),
      'awaiting_approval', count(*) FILTER (WHERE status = 'awaiting_approval'),
      'oldest_queued_seconds', COALESCE(EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued')), 0),
      'succeeded_24h', count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - interval '24 hours'),
      'failed_24h', count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '24 hours'),
      'p50_seconds_24h', percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finished_at - started_at))
          FILTER (WHERE status = 'succeeded' AND finished_at > now() - interval '24 hours'),
      'p95_seconds_24h', percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finished_at - started_at))
          FILTER (WHERE status = 'succeeded' AND finished_at > now() - interval '24 hours'))
    FROM agent_runs),
  'documents', (SELECT jsonb_build_object(
      'pending', count(*) FILTER (WHERE status = 'pending'),
      'processing', count(*) FILTER (WHERE status = 'processing'),
      'failed', count(*) FILTER (WHERE status = 'failed'),
      'oldest_pending_seconds', COALESCE(EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'pending')), 0))
    FROM documents),
  'graph_builds', (SELECT jsonb_build_object(
      'pending', count(*) FILTER (WHERE status = 'pending'),
      'running', count(*) FILTER (WHERE status = 'running'))
    FROM kg_builds),
  'purge_jobs', (SELECT jsonb_build_object(
      'pending', count(*),
      'retrying', count(*) FILTER (WHERE attempts > 0),
      'max_attempts', COALESCE(max(attempts), 0),
      'oldest_seconds', COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0))
    FROM purge_jobs),
  'workers_alive', (SELECT count(*) FROM worker_heartbeats WHERE last_seen_at > now() - interval '30 seconds'),
  'busy_workers', (SELECT count(DISTINCT lease_owner) FROM (
      SELECT lease_owner FROM agent_runs WHERE status = 'running' AND lease_expires_at > now()
      UNION ALL SELECT lease_owner FROM documents WHERE status = 'processing' AND lease_expires_at > now()
      UNION ALL SELECT lease_owner FROM kg_builds WHERE status = 'running' AND lease_expires_at > now()
      UNION ALL SELECT lease_owner FROM purge_jobs WHERE status = 'running' AND lease_expires_at > now()) t
    WHERE lease_owner <> 'inline')
)
$$;
REVOKE ALL ON FUNCTION system_status() FROM PUBLIC;
"""


def upgrade() -> None:
    op.create_table(
        'worker_heartbeats',
        sa.Column('owner', sa.String(length=64), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('owner'),
    )
    op.execute(FUNCTION)


def downgrade() -> None:
    # 恢复 0014 的函数：已发布的迁移文件不会再改，直接取它的 FUNCTION
    previous = runpy.run_path(str(Path(__file__).with_name("0014_system_status.py")))["FUNCTION"]
    op.execute(previous)
    op.drop_table('worker_heartbeats')
