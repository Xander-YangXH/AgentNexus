"""删除后的清理任务（阶段 7.1，decisions/0026）

purge_jobs 是删除的“发件箱”：删除知识库 / 文档的事务里插入一条，worker 据此清理 Qdrant、原件和 Neo4j。
不加外键：被删的行已经不在了。不启用 RLS：api 只插入，不读；worker 用 BYPASSRLS 角色处理。

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'purge_jobs',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=True),
        sa.Column('blob_keys', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
        sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('lease_owner', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.CheckConstraint("kind IN ('document', 'knowledge_base')", name='ck_purge_jobs_kind'),
        sa.CheckConstraint("status IN ('pending', 'running')", name='ck_purge_jobs_status'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_purge_jobs_status', 'purge_jobs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_purge_jobs_status', table_name='purge_jobs')
    op.drop_table('purge_jobs')
