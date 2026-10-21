"""agent_runs 记录 checkpoint 是否已清理，并为“待清理”建部分索引（decisions/0010）

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("checkpoint_purged_at", sa.DateTime(timezone=True), nullable=True))
    # 部分索引：只索引“已结束、还没清理”的行。绝大多数 run 清理后就不在索引里了，所以它始终很小
    op.create_index(
        "ix_agent_runs_checkpoint_purge_candidates",
        "agent_runs",
        ["finished_at"],
        postgresql_where=sa.text("finished_at IS NOT NULL AND checkpoint_purged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_checkpoint_purge_candidates", table_name="agent_runs")
    op.drop_column("agent_runs", "checkpoint_purged_at")
