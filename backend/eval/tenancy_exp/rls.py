"""实验 2：数据隔离——只在应用层过滤 vs 再加 PostgreSQL 行级安全（RLS）作为第二道防线（decisions/0023 草稿）。

模型：部门 / 用户 / 知识库（private 只有自己可见，department 同部门可见）/ 文档。
测：
1. 性能：列出可见知识库、按 id 取文档，应用层 WHERE 与 RLS 的耗时对比；
2. 价值：应用代码“忘了加过滤条件”时，两种方式的结果；
3. 陷阱：超级用户 / 表所有者绕过 RLS；会话级 SET 在连接池里串到下一个请求；没设置用户时的行为。
用法：BENCH_PG_URL=postgresql://<超级用户>@localhost:5432/postgres python rls.py
"""

import os
import random
import statistics
import time
import uuid

import psycopg

ADMIN = os.environ["BENCH_PG_URL"]
DB = "bench_tenancy"
URL = psycopg.conninfo.make_conninfo(ADMIN, dbname=DB)
APP = psycopg.conninfo.make_conninfo(ADMIN, dbname=DB, user="tenancy_app", password="app-pass-for-bench")
DEPTS, USERS, KBS, DOCS = 20, 2000, 50_000, 300_000

SCHEMA = """
CREATE TABLE departments (id uuid PRIMARY KEY, name text);
CREATE TABLE users (id uuid PRIMARY KEY, department_id uuid REFERENCES departments);
CREATE TABLE knowledge_bases (id uuid PRIMARY KEY, owner_id uuid REFERENCES users, department_id uuid,
                              visibility text NOT NULL, name text);
CREATE INDEX ON knowledge_bases (owner_id);
CREATE INDEX ON knowledge_bases (department_id) WHERE visibility = 'department';
CREATE TABLE documents (id uuid PRIMARY KEY, knowledge_base_id uuid REFERENCES knowledge_bases, body text);
CREATE INDEX ON documents (knowledge_base_id);
"""

# 当前用户与部门放在事务级设置里；没设置时 current_setting(..., true) 返回 NULL，比较结果为 NULL → 不可见（默认拒绝）
POLICIES = """
CREATE FUNCTION app_user() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_dept() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.department_id', true), '')::uuid $$;
ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY;
CREATE POLICY kb_visible ON knowledge_bases USING (
  owner_id = app_user() OR (visibility = 'department' AND department_id = app_dept()));
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY doc_visible ON documents USING (
  EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.id = knowledge_base_id));  -- 子查询本身也受 kb 的策略约束
"""


def setup():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
        c.execute("DROP ROLE IF EXISTS tenancy_app")
        c.execute("CREATE ROLE tenancy_app LOGIN PASSWORD 'app-pass-for-bench' NOSUPERUSER NOBYPASSRLS")
    rnd = random.Random(7)
    depts = [uuid.uuid4() for _ in range(DEPTS)]
    users = [(uuid.uuid4(), rnd.choice(depts)) for _ in range(USERS)]
    kbs = []
    for _ in range(KBS):
        u, d = rnd.choice(users)
        kbs.append((uuid.uuid4(), u, d, "department" if rnd.random() < 0.3 else "private"))
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(SCHEMA)
        with c.cursor() as cur:
            with cur.copy("COPY departments (id, name) FROM STDIN") as cp:
                for d in depts: cp.write_row((d, "d"))
            with cur.copy("COPY users (id, department_id) FROM STDIN") as cp:
                for u in users: cp.write_row(u)
            with cur.copy("COPY knowledge_bases (id, owner_id, department_id, visibility, name) FROM STDIN") as cp:
                for k in kbs: cp.write_row((*k, "kb"))
            with cur.copy("COPY documents (id, knowledge_base_id, body) FROM STDIN") as cp:
                for _ in range(DOCS): cp.write_row((uuid.uuid4(), rnd.choice(kbs)[0], "x" * 50))
        c.execute(POLICIES)
        c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenancy_app")
        c.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO tenancy_app")
        c.execute("ANALYZE")
    return users, kbs


def bench(conn, sql, params_fn, n=300, as_user=None):
    times = []
    for _ in range(n):
        params, (u, d) = params_fn()
        with conn.transaction():
            if as_user:
                conn.execute("SELECT set_config('app.user_id', %s, true), set_config('app.department_id', %s, true)",
                             (str(u), str(d)))
            t = time.perf_counter()
            conn.execute(sql, params).fetchall()
            times.append(time.perf_counter() - t)
    return statistics.median(times) * 1000, sorted(times)[int(n * 0.95)] * 1000


def main():
    users, kbs = setup()
    rnd = random.Random(1)
    print(f"数据：{DEPTS} 个部门、{USERS} 个用户、{KBS} 个知识库（30% 部门可见）、{DOCS} 篇文档")
    with psycopg.connect(URL) as admin, psycopg.connect(APP) as app:
        def user():
            return rnd.choice(users)

        print("\n1. 性能（中位数 / p95，毫秒）")
        list_where = ("SELECT id, name FROM knowledge_bases WHERE owner_id = %s OR (visibility = 'department' AND department_id = %s)")
        def where_params():
            u = user()
            return (u[0], u[1]), u

        print("  列出可见知识库  应用层 WHERE：%.2f / %.2f" % bench(admin, list_where, where_params))
        print("  列出可见知识库  RLS          ：%.2f / %.2f" % bench(app, "SELECT id, name FROM knowledge_bases", lambda: ((), user()), as_user=True))
        docs = [r[0] for r in admin.execute("SELECT id FROM documents TABLESAMPLE SYSTEM (1) LIMIT 300").fetchall()]
        get_where = ("SELECT d.id FROM documents d JOIN knowledge_bases k ON k.id = d.knowledge_base_id WHERE d.id = %s "
                     "AND (k.owner_id = %s OR (k.visibility = 'department' AND k.department_id = %s))")
        print("  按 id 取文档    应用层 WHERE：%.2f / %.2f" % bench(admin, get_where, lambda: ((lambda u: ((rnd.choice(docs), u[0], u[1]), u))(user()))))
        print("  按 id 取文档    RLS          ：%.2f / %.2f" % bench(app, "SELECT id FROM documents WHERE id = %s", lambda: ((rnd.choice(docs),), user()), as_user=True))

        print("\n2. 应用代码忘了加过滤条件：SELECT count(*) FROM knowledge_bases")
        u, d = users[0]
        mine = admin.execute(list_where.replace("id, name", "count(*)"), (u, d)).fetchone()[0]
        print(f"  该用户应当可见：{mine}")
        print(f"  只靠应用层（漏写 WHERE）：{admin.execute('SELECT count(*) FROM knowledge_bases').fetchone()[0]}  ← 全部泄露")
        with app.transaction():
            app.execute("SELECT set_config('app.user_id', %s, true), set_config('app.department_id', %s, true)", (str(u), str(d)))
            print(f"  有 RLS（漏写 WHERE）：{app.execute('SELECT count(*) FROM knowledge_bases').fetchone()[0]}")

        print("\n3. 陷阱")
        with admin.transaction():
            admin.execute("SELECT set_config('app.user_id', %s, true)", (str(u),))
            n = admin.execute("SELECT count(*) FROM knowledge_bases").fetchone()[0]
        is_super = admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        print(f"  a) 以超级用户 / 表所有者连接（superuser={is_super}），设置了用户：看到 {n} 行  ← RLS 不生效")
        with app.transaction():
            n = app.execute("SELECT count(*) FROM knowledge_bases").fetchone()[0]
        print(f"  b) 应用角色没设置用户：看到 {n} 行（默认拒绝）")
        # c) 会话级 SET：同一个连接被下一个请求复用时，上一个用户的身份还在
        app.execute("SELECT set_config('app.user_id', %s, false), set_config('app.department_id', %s, false)", (str(u), str(d)))
        app.commit()
        with app.transaction():  # 下一个“请求”忘了设置身份
            n = app.execute("SELECT count(*) FROM knowledge_bases").fetchone()[0]
        print(f"  c) 上一个请求用会话级 SET（is_local=false），下一个请求没设置：看到 {n} 行  ← 串到了上一个用户")
        app.execute("RESET app.user_id"); app.execute("RESET app.department_id"); app.commit()
        with app.transaction():
            app.execute("SELECT set_config('app.user_id', %s, true)", (str(u),))
        with app.transaction():
            n = app.execute("SELECT count(*) FROM knowledge_bases").fetchone()[0]
        print(f"     改用事务级（is_local=true），下一个事务没设置：看到 {n} 行")
        with app.transaction():
            app.execute("SELECT set_config('app.user_id', %s, true), set_config('app.department_id', %s, true)", (str(u), str(d)))
            # 我原以为“只写 USING 的策略不检查新写入的行”，实测不对：没写 WITH CHECK 时，USING 同时用作写入检查
            try:
                app.execute("INSERT INTO knowledge_bases VALUES (%s, %s, %s, 'private', 'forged')", (uuid.uuid4(), users[1][0], users[1][1]))
                print("  d) 以别人的身份插入：成功")
            except psycopg.errors.InsufficientPrivilege as e:
                print(f"  d) 以别人的身份插入：被拒（{type(e).__name__}：没写 WITH CHECK 时 USING 也用于检查新行）")


if __name__ == "__main__":
    main()
