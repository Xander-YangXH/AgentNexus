"""对话关联知识库、助手消息的引用快照（阶段 2.4，decisions/0018）

messages 加一个可空列：PostgreSQL 加可空、无默认值的列只改表结构、不重写数据，大表上也很快。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 11:15:10.519657
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('conversation_knowledge_bases',
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('conversation_id', 'knowledge_base_id')
    )
    op.create_index(op.f('ix_conversation_knowledge_bases_knowledge_base_id'), 'conversation_knowledge_bases', ['knowledge_base_id'], unique=False)
    op.add_column('messages', sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('messages', 'citations')
    op.drop_index(op.f('ix_conversation_knowledge_bases_knowledge_base_id'), table_name='conversation_knowledge_bases')
    op.drop_table('conversation_knowledge_bases')
