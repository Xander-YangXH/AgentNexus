"""深度调研：对话的 Agent 类型、run 的审批状态、产物表、消息附件（阶段 4.3，decisions/0022）

- agent_runs.status 从 varchar(16) 加宽到 24：新状态 awaiting_approval 有 17 个字符，原长度放不下；
- “活跃 run”的部分唯一索引和状态检查约束要按新的状态集合重建（等待审批也算活跃）；
- 新列都可为空或带常量默认值：不重写已有数据。

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

OLD_STATUSES = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
NEW_STATUSES = OLD_STATUSES + ", 'awaiting_approval'"


def upgrade() -> None:
    op.add_column('conversations', sa.Column('agent', sa.String(length=16), server_default='chat', nullable=False))
    op.create_check_constraint('ck_conversations_agent', 'conversations', "agent IN ('chat', 'research')")
    op.add_column('messages', sa.Column('attachments', postgresql.JSONB(), nullable=True))

    op.alter_column('agent_runs', 'status', type_=sa.String(length=24), existing_type=sa.String(length=16))
    op.add_column('agent_runs', sa.Column('approval_request', postgresql.JSONB(), nullable=True))
    op.add_column('agent_runs', sa.Column('approval_response', postgresql.JSONB(), nullable=True))
    op.add_column('agent_runs', sa.Column('approval_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('ck_agent_runs_status', 'agent_runs', type_='check')
    op.create_check_constraint('ck_agent_runs_status', 'agent_runs', f"status IN ({NEW_STATUSES})")
    op.drop_index('uq_agent_runs_one_active_per_conversation', table_name='agent_runs')
    op.create_index('uq_agent_runs_one_active_per_conversation', 'agent_runs', ['conversation_id'], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running', 'awaiting_approval')"))

    op.create_table(
        'artifacts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('blob_key', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artifacts_conversation', 'artifacts', ['conversation_id', 'created_at'])
    op.create_index('ix_artifacts_user_id', 'artifacts', ['user_id'])
    op.create_index('ix_artifacts_run_id', 'artifacts', ['run_id'])


def downgrade() -> None:
    op.drop_table('artifacts')
    op.execute("UPDATE agent_runs SET status = 'cancelled', finished_at = now() WHERE status = 'awaiting_approval'")
    op.drop_index('uq_agent_runs_one_active_per_conversation', table_name='agent_runs')
    op.create_index('uq_agent_runs_one_active_per_conversation', 'agent_runs', ['conversation_id'], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_constraint('ck_agent_runs_status', 'agent_runs', type_='check')
    op.create_check_constraint('ck_agent_runs_status', 'agent_runs', f"status IN ({OLD_STATUSES})")
    op.drop_column('agent_runs', 'approval_requested_at')
    op.drop_column('agent_runs', 'approval_response')
    op.drop_column('agent_runs', 'approval_request')
    op.alter_column('agent_runs', 'status', type_=sa.String(length=16), existing_type=sa.String(length=24))
    op.drop_column('messages', 'attachments')
    op.drop_constraint('ck_conversations_agent', 'conversations', type_='check')
    op.drop_column('conversations', 'agent')
