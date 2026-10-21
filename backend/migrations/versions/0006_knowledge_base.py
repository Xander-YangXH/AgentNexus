"""知识库：knowledge_bases / documents / document_chunks（阶段 2，decisions/0014、0015）

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 18:45:24.588131
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('knowledge_bases',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'name', name='uq_knowledge_bases_user_name')
    )
    op.create_index(op.f('ix_knowledge_bases_user_id'), 'knowledge_bases', ['user_id'], unique=False)
    op.create_table('documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('blob_key', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=32), nullable=True),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'processing', 'ready', 'failed')", name='ck_documents_status'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('knowledge_base_id', 'sha256', name='uq_documents_kb_sha256')
    )
    op.create_index('ix_documents_kb_created', 'documents', ['knowledge_base_id', 'created_at'], unique=False)
    op.create_index('ix_documents_status', 'documents', ['status'], unique=False)
    op.create_table('document_chunks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'seq', name='uq_document_chunks_doc_seq')
    )
    op.create_index(op.f('ix_document_chunks_knowledge_base_id'), 'document_chunks', ['knowledge_base_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_document_chunks_knowledge_base_id'), table_name='document_chunks')
    op.drop_table('document_chunks')
    op.drop_index('ix_documents_status', table_name='documents')
    op.drop_index('ix_documents_kb_created', table_name='documents')
    op.drop_table('documents')
    op.drop_index(op.f('ix_knowledge_bases_user_id'), table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
