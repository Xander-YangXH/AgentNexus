"""agent_runs 增加 error_code：对外只暴露错误码，原始报错留在 error 列供排查（decisions/0009）

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空、无默认值：只加列不改已有行，PostgreSQL 只改元数据，不重写表，大表上也是瞬间完成
    # 不加 CHECK 约束：错误码会随接入的供应商增加，由代码里的枚举约束即可
    op.add_column("agent_runs", sa.Column("error_code", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "error_code")
