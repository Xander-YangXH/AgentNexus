"""知识图谱：构建记录、抽取缓存、实体、关系、出处（阶段 3，decisions/0019）

PostgreSQL 是事实来源；Neo4j 里的图由每次构建的最后一步同步过去，可以整体重建。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22 13:03:58.833837
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('kg_builds',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.Column('unique_count', sa.Integer(), nullable=False),
    sa.Column('extract_count', sa.Integer(), nullable=False),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=False),
    sa.Column('done_count', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('entity_count', sa.Integer(), nullable=False),
    sa.Column('relation_count', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed')", name='ck_kg_builds_status'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_kg_builds_kb_created', 'kg_builds', ['knowledge_base_id', 'created_at'], unique=False)
    op.create_index('uq_kg_builds_one_active_per_kb', 'kg_builds', ['knowledge_base_id'], unique=True, postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.create_table('kg_entities',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('mention_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('knowledge_base_id', 'key', name='uq_kg_entities_kb_key')
    )
    op.create_table('kg_extractions',
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.Integer(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('knowledge_base_id', 'text_sha256', 'prompt_version')
    )
    op.create_table('kg_relations',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('target_id', sa.UUID(), nullable=False),
    sa.Column('predicate', sa.String(length=32), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('mention_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['kg_entities.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['kg_entities.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'target_id', 'predicate', name='uq_kg_relations_edge')
    )
    op.create_index(op.f('ix_kg_relations_knowledge_base_id'), 'kg_relations', ['knowledge_base_id'], unique=False)
    op.create_index(op.f('ix_kg_relations_target_id'), 'kg_relations', ['target_id'], unique=False)
    op.create_table('kg_mentions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('chunk_id', sa.UUID(), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=True),
    sa.Column('relation_id', sa.UUID(), nullable=True),
    sa.CheckConstraint('(entity_id IS NULL) <> (relation_id IS NULL)', name='ck_kg_mentions_one_target'),
    sa.ForeignKeyConstraint(['chunk_id'], ['document_chunks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['entity_id'], ['kg_entities.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['relation_id'], ['kg_relations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_kg_mentions_chunk_id'), 'kg_mentions', ['chunk_id'], unique=False)
    op.create_index(op.f('ix_kg_mentions_entity_id'), 'kg_mentions', ['entity_id'], unique=False)
    op.create_index(op.f('ix_kg_mentions_knowledge_base_id'), 'kg_mentions', ['knowledge_base_id'], unique=False)
    op.create_index(op.f('ix_kg_mentions_relation_id'), 'kg_mentions', ['relation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_kg_mentions_relation_id'), table_name='kg_mentions')
    op.drop_index(op.f('ix_kg_mentions_knowledge_base_id'), table_name='kg_mentions')
    op.drop_index(op.f('ix_kg_mentions_entity_id'), table_name='kg_mentions')
    op.drop_index(op.f('ix_kg_mentions_chunk_id'), table_name='kg_mentions')
    op.drop_table('kg_mentions')
    op.drop_index(op.f('ix_kg_relations_target_id'), table_name='kg_relations')
    op.drop_index(op.f('ix_kg_relations_knowledge_base_id'), table_name='kg_relations')
    op.drop_table('kg_relations')
    op.drop_table('kg_extractions')
    op.drop_table('kg_entities')
    op.drop_index('uq_kg_builds_one_active_per_kb', table_name='kg_builds', postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_index('ix_kg_builds_kb_created', table_name='kg_builds')
    op.drop_table('kg_builds')
