"""对话管理：归档、最近活动时间（排序用）及对应的部分索引（decisions/0012）

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    # NOT NULL + 默认值 now()：PostgreSQL 11 起，带非易变默认值的加列只改元数据、不重写表；
    # now() 在一条语句内取同一个值，属于这种情况。随后再用真实数据回填
    op.add_column(
        "conversations",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # 回填：最后一条消息的时间；没有消息就用创建时间（数据量大时应分批更新，这里数据量很小）
    op.execute(
        """
        UPDATE conversations c
        SET last_activity_at = COALESCE(
            (SELECT max(m.created_at) FROM messages m WHERE m.conversation_id = c.id),
            c.created_at
        )
        """
    )
    # 对话列表的查询是“某用户、未归档、按最近活动倒序”：部分索引只收未归档的对话，并且直接按需要的顺序排好
    op.create_index(
        "ix_conversations_user_recent_active",
        "conversations",
        ["user_id", sa.text("last_activity_at DESC")],
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_user_recent_active", table_name="conversations")
    op.drop_column("conversations", "last_activity_at")
    op.drop_column("conversations", "archived_at")
