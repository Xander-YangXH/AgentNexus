"""登录、部门、角色、知识库共享、用量明细，以及行级安全策略（阶段 5，decisions/0023）

行级安全（RLS）是第二道防线：服务层已经做了归属 / 可见性检查，RLS 让“漏写过滤条件”的查询也只能看到该看的行。
- 当前用户、部门、角色由应用在每个事务开始时用 set_config(..., true)（事务级）设置；没设置时函数返回 NULL，策略默认拒绝。
- 只有 api 进程使用受 RLS 约束的普通角色；迁移用表所有者（不受约束），worker 用 BYPASSRLS 角色——它执行的任务按 run 归属取数据。
  数据库角色本身不在迁移里创建（属于部署配置，密码来自环境变量）：见 agentnexus/storage/roles.py。
- users / departments / user_sessions 不启用 RLS：登录发生在“知道当前用户是谁”之前；接口层按角色检查。

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

KB_CHILD_TABLES = ("documents", "document_chunks", "kg_builds", "kg_extractions", "kg_entities", "kg_relations", "kg_mentions")
CONVERSATION_CHILD_TABLES = ("messages", "agent_runs")
RLS_TABLES = ("knowledge_bases", *KB_CHILD_TABLES, "conversations", *CONVERSATION_CHILD_TABLES,
              "conversation_knowledge_bases", "artifacts", "usage_events")

FUNCTIONS = """
CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_department_id() RETURNS uuid LANGUAGE sql STABLE
  AS $$ SELECT nullif(current_setting('app.department_id', true), '')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE
  AS $$ SELECT coalesce(current_setting('app.role', true), '') $$;
"""

# 子查询里的 knowledge_bases / conversations 本身也受 RLS 约束：“看得见父行”就等于“看得见子行”
KB_VISIBLE = "EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.id = knowledge_base_id)"
KB_OWNED = "EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.id = knowledge_base_id AND k.user_id = app_user_id())"
CONV_OWNED = "EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_id)"


def upgrade() -> None:
    op.create_table(
        'departments',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('role', sa.String(length=16), server_default='member', nullable=False))
    op.add_column('users', sa.Column('department_id', sa.UUID(), nullable=True))
    op.add_column('users', sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(None, 'users', 'departments', ['department_id'], ['id'])
    op.create_index('ix_users_department_id', 'users', ['department_id'])
    op.create_check_constraint('ck_users_role', 'users', "role IN ('admin', 'manager', 'member')")

    op.create_table(
        'user_sessions',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_user_sessions_user_id', 'user_sessions', ['user_id'])

    op.add_column('knowledge_bases', sa.Column('visibility', sa.String(length=16), server_default='private', nullable=False))
    op.add_column('knowledge_bases', sa.Column('department_id', sa.UUID(), nullable=True))
    op.create_foreign_key(None, 'knowledge_bases', 'departments', ['department_id'], ['id'])
    op.create_check_constraint('ck_knowledge_bases_visibility', 'knowledge_bases', "visibility IN ('private', 'department')")
    op.create_index('ix_knowledge_bases_department_shared', 'knowledge_bases', ['department_id'],
                    postgresql_where=sa.text("visibility = 'department'"))

    op.create_table(
        'usage_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('department_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('input_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cached_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.CheckConstraint("kind IN ('run', 'llm', 'search')", name='ck_usage_events_kind'),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_events_user_time', 'usage_events', ['user_id', 'created_at'])
    op.create_index('ix_usage_events_department_time', 'usage_events', ['department_id', 'created_at'])
    op.create_index('ix_usage_events_time', 'usage_events', ['created_at'])

    # ---------- 行级安全 ----------
    op.execute(FUNCTIONS)
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # 知识库：所有者，或部门共享且同部门，可以读；只有所有者能写。多条宽松策略之间是“或”的关系
    op.execute("""CREATE POLICY kb_read ON knowledge_bases FOR SELECT USING (
        user_id = app_user_id() OR (visibility = 'department' AND department_id = app_department_id()))""")
    op.execute("CREATE POLICY kb_write ON knowledge_bases FOR ALL USING (user_id = app_user_id()) "
               "WITH CHECK (user_id = app_user_id())")
    for table in KB_CHILD_TABLES:
        op.execute(f"CREATE POLICY {table}_read ON {table} FOR SELECT USING ({KB_VISIBLE})")
        op.execute(f"CREATE POLICY {table}_write ON {table} FOR ALL USING ({KB_OWNED}) WITH CHECK ({KB_OWNED})")
    op.execute("CREATE POLICY conversations_owner ON conversations FOR ALL USING (user_id = app_user_id()) "
               "WITH CHECK (user_id = app_user_id())")
    for table in CONVERSATION_CHILD_TABLES:
        op.execute(f"CREATE POLICY {table}_owner ON {table} FOR ALL USING ({CONV_OWNED}) WITH CHECK ({CONV_OWNED})")
    # 关联知识库：对话是自己的，且（写入时）知识库看得见
    op.execute(f"CREATE POLICY ckb_owner ON conversation_knowledge_bases FOR ALL USING ({CONV_OWNED}) "
               f"WITH CHECK ({CONV_OWNED} AND {KB_VISIBLE})")
    op.execute("CREATE POLICY artifacts_owner ON artifacts FOR ALL USING (user_id = app_user_id()) "
               "WITH CHECK (user_id = app_user_id())")
    # 用量：本人；部门负责人看本部门；管理员看全部。api 只写自己的（检索、请求），模型调用由 worker 写
    op.execute("""CREATE POLICY usage_read ON usage_events FOR SELECT USING (
        user_id = app_user_id() OR app_role() = 'admin'
        OR (app_role() = 'manager' AND department_id = app_department_id()))""")
    op.execute("CREATE POLICY usage_insert ON usage_events FOR INSERT WITH CHECK (user_id = app_user_id())")


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table('usage_events')
    for table in KB_CHILD_TABLES:
        op.execute(f"DROP POLICY {table}_read ON {table}")
        op.execute(f"DROP POLICY {table}_write ON {table}")
    for table in CONVERSATION_CHILD_TABLES:
        op.execute(f"DROP POLICY {table}_owner ON {table}")
    op.execute("DROP POLICY kb_read ON knowledge_bases")
    op.execute("DROP POLICY kb_write ON knowledge_bases")
    op.execute("DROP POLICY conversations_owner ON conversations")
    op.execute("DROP POLICY ckb_owner ON conversation_knowledge_bases")
    op.execute("DROP POLICY artifacts_owner ON artifacts")
    op.execute("DROP FUNCTION app_user_id(), app_department_id(), app_role()")
    op.drop_index('ix_knowledge_bases_department_shared', table_name='knowledge_bases')
    op.drop_constraint('ck_knowledge_bases_visibility', 'knowledge_bases', type_='check')
    op.drop_constraint('knowledge_bases_department_id_fkey', 'knowledge_bases', type_='foreignkey')
    op.drop_column('knowledge_bases', 'department_id')
    op.drop_column('knowledge_bases', 'visibility')
    op.drop_table('user_sessions')
    op.drop_constraint('ck_users_role', 'users', type_='check')
    op.drop_index('ix_users_department_id', table_name='users')
    op.drop_constraint('users_department_id_fkey', 'users', type_='foreignkey')
    op.drop_column('users', 'disabled_at')
    op.drop_column('users', 'department_id')
    op.drop_column('users', 'role')
    op.drop_column('users', 'password_hash')
    op.drop_table('departments')
