"""清理任务也用于删除对话（阶段 7.3，decisions/0028）

- kind 增加 conversation；知识库 id 改为可空（删除对话的任务没有知识库）；
- conversation_id：删除对话时用来删沙盒工作空间（工作空间按对话划分，0021）；
- run_ids：还没被清理的 LangGraph checkpoint（thread_id = run_id，0006）。

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('purge_jobs', 'knowledge_base_id', existing_type=sa.UUID(), nullable=True)
    op.add_column('purge_jobs', sa.Column('conversation_id', sa.UUID(), nullable=True))
    op.add_column('purge_jobs', sa.Column('run_ids', postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"),
                                          nullable=False))
    op.drop_constraint('ck_purge_jobs_kind', 'purge_jobs', type_='check')
    op.create_check_constraint('ck_purge_jobs_kind', 'purge_jobs', "kind IN ('document', 'knowledge_base', 'conversation')")


def downgrade() -> None:
    op.execute("DELETE FROM purge_jobs WHERE kind = 'conversation'")
    op.drop_constraint('ck_purge_jobs_kind', 'purge_jobs', type_='check')
    op.create_check_constraint('ck_purge_jobs_kind', 'purge_jobs', "kind IN ('document', 'knowledge_base')")
    op.drop_column('purge_jobs', 'run_ids')
    op.drop_column('purge_jobs', 'conversation_id')
    op.alter_column('purge_jobs', 'knowledge_base_id', existing_type=sa.UUID(), nullable=False)
