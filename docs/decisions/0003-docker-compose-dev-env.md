# 0003 - 本地开发环境：Docker Compose + healthcheck

**状态**：已决定（阶段 0）

**背景**：项目依赖 Postgres、Redis，后续还会叠加 Qdrant、MinIO、Neo4j、worker。需要新人 `git clone` 后一条命令可复现环境，且服务启动顺序不能靠"多试几次"。

**决定**：
- 用 `docker-compose.yml` 编排，有状态服务（postgres、redis）配 healthcheck，api 以 `depends_on.condition: service_healthy` 等待。
- 配置走 `.env`（提交 `.env.template`，`.env` 已 gitignore），连接串在 compose 里用服务名拼装，应用代码只读环境变量。
- 数据放命名卷（`pg_data`、`redis_data`）。
- 暴露 `/health`（存活，不查依赖）和 `/health/ready`（就绪，查 Postgres 与 Redis）。
- 宿主机端口仅绑定 `127.0.0.1`。

**后果**：启动顺序可靠、环境可复现；代价是首次需要构建镜像、依赖 Docker Desktop。`docker compose down -v` 会删除数据卷，需在文档里提示。

**验证**：`docker compose up -d --build` 后 `curl localhost:8000/health` 返回 200，`/health/ready` 返回 postgres/redis 均 ok。已于 2026-10-17 在本机验证：三个服务均 healthy；`/health` 200，`/health/ready` 返回 postgres/redis 均 ok；停掉 postgres 后 `/health` 仍 200、`/health/ready` 变 503，恢复后回到 ok；api 容器内可按服务名解析 postgres/redis。
