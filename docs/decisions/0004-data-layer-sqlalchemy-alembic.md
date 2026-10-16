# 0004 - 数据层：SQLAlchemy 2.0 异步 + Alembic + psycopg3，run 与消息同事务

**状态**：已决定（阶段 1）

**背景**：阶段 1 要把用户、对话、消息、AgentRun 落库，并实现"先落库再投递"。需要选 ORM/迁移工具，并确定哪些约束放数据库、哪些放代码。

**决定**：
- ORM 用 SQLAlchemy 2.0 异步，驱动用 psycopg3（与 LangGraph 的 PostgresSaver 同一驱动，整个项目只维护一套 Postgres 驱动）；迁移用 Alembic（同步引擎，迁移是一次性脚本，不需要异步）。
- 代码分层：`storage/postgres`（表定义与会话）→ `repositories`（单表读写，不管事务）→ `services`（用例，一个方法一个事务）。
- `submit()` 把用户消息和 `queued` 状态的 AgentRun 放在同一个事务里提交；助手回复落库与 run 置为 `succeeded` 同一事务。
- 状态转移用条件更新（`UPDATE ... WHERE status=... `+ rowcount）实现原子抢占，不用先读后写。
- 不变量放数据库：部分唯一索引保证"同一对话同一时刻最多一个活跃（queued/running）run"；status/role 用 CHECK 约束；消息顺序用自增 `seq`，不依赖 `created_at`（同事务内相同）。
- 迁移在 compose 里由一次性 `migrate` 服务执行，api 以 `service_completed_successfully` 等它。

**后果**：并发下的正确性由数据库保证而不是应用层加锁，多个 api/worker 实例天然安全；代价是引入 ORM 与迁移的学习成本，且集成测试必须跑在真实 Postgres 上（部分唯一索引、条件更新 SQLite 模拟不了）。阶段 4 的 lease 字段将通过新迁移追加。

**验证**：`backend/tests/test_run_service.py` 8 个集成测试通过（原子提交、失败回滚无孤儿消息、8 路并发 claim 只有 1 个成功、终态不可覆盖等）；`docker compose up` 后 `migrate` 退出码 0、五张表建成、重复 up 迁移幂等。
