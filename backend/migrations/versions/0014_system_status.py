"""运行状态的聚合函数（阶段 7.8，decisions/0031）

业务表就是任务表（0020），所以队列积压、失败、清理重试、执行中的 worker 都能直接从表里聚合出来。
api 的数据库角色受 RLS 约束（0023），直接查只能数到“当前用户”的行；这里用 SECURITY DEFINER 函数
（以表所有者的身份执行，绕过 RLS），而且**只返回计数和时长，不返回任何行**。search_path 固定，防止被劫持。

Revision ID: 0014
Revises: 0013
"""
from alembic import op


revision = '0014'
down_revision = '0013'
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
    op.execute(FUNCTION)  # 授权给 api / worker 角色由 storage/roles.py 在迁移后完成（GRANT EXECUTE ON ALL FUNCTIONS）


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS system_status()")
