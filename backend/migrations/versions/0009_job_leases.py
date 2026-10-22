"""任务租约：agent_runs / documents / kg_builds 加租约字段，agent_runs 加取消请求（阶段 4.1，decisions/0020）

加带常量默认值的 NOT NULL 列：PostgreSQL 11 起只改表结构、不重写数据。
已经在执行中、但没有租约的旧记录（lease_expires_at 为空）在认领时按“租约已过期”处理，升级后会被 worker 接管。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 13:40:43.932242
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agent_runs', sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_runs', sa.Column('lease_owner', sa.String(length=64), nullable=True))
    op.add_column('agent_runs', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_runs', sa.Column('lease_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('agent_runs', sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('documents', sa.Column('lease_owner', sa.String(length=64), nullable=True))
    op.add_column('documents', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('documents', sa.Column('lease_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('documents', sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('kg_builds', sa.Column('lease_owner', sa.String(length=64), nullable=True))
    op.add_column('kg_builds', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('kg_builds', sa.Column('lease_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('kg_builds', sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('kg_builds', 'attempts')
    op.drop_column('kg_builds', 'lease_version')
    op.drop_column('kg_builds', 'lease_expires_at')
    op.drop_column('kg_builds', 'lease_owner')
    op.drop_column('documents', 'attempts')
    op.drop_column('documents', 'lease_version')
    op.drop_column('documents', 'lease_expires_at')
    op.drop_column('documents', 'lease_owner')
    op.drop_column('agent_runs', 'attempts')
    op.drop_column('agent_runs', 'lease_version')
    op.drop_column('agent_runs', 'lease_expires_at')
    op.drop_column('agent_runs', 'lease_owner')
    op.drop_column('agent_runs', 'cancel_requested_at')
