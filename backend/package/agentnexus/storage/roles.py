"""数据库角色（decisions/0023）：api 用受 RLS 约束的普通角色，worker 用 BYPASSRLS 角色，迁移用表所有者。

为什么不放进 Alembic 迁移：角色是整个 PostgreSQL 实例级别的部署配置，密码来自环境变量，不应该写进迁移历史。
compose 的 migrate 服务在建表之后执行本模块（以超级用户 / 所有者身份）：创建或更新角色、授予表权限。幂等。

实验（backend/eval/tenancy_exp/rls.py）：超级用户和表所有者不受 RLS 约束——应用继续用 postgres 镜像建的超级用户连接，
策略写得再对也不生效。
"""

import os

import psycopg
from psycopg import sql


def ensure_roles(admin_url: str, *, api_role: str, api_password: str, worker_role: str, worker_password: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        database = conn.info.dbname
        for role, password, bypass in ((api_role, api_password, False), (worker_role, worker_password, True)):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE {} PASSWORD {}").format(
                sql.SQL(verb), sql.Identifier(role), sql.SQL("BYPASSRLS" if bypass else "NOBYPASSRLS"),
                sql.Literal(password)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}")
                         .format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {}").format(sql.Identifier(role)))


def main() -> None:
    ensure_roles(
        os.environ["DATABASE_URL"],
        api_role=os.environ.get("DB_API_ROLE", "agentnexus_api"),
        api_password=os.environ["DB_API_PASSWORD"],
        worker_role=os.environ.get("DB_WORKER_ROLE", "agentnexus_worker"),
        worker_password=os.environ["DB_WORKER_PASSWORD"],
    )
    print("database roles ready")


if __name__ == "__main__":
    main()
