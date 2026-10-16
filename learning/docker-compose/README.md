# Docker Compose 学习 Playground

阶段 0 最后一块学习内容：多服务编排。纯练习，不是 AgentNexus 产品代码；学完对照仓库根目录的 `docker-compose.yml`（阶段 0 交付物）看一遍。

> 需要先启动 Docker Desktop（`docker info` 能正常输出即可）。

## 四个核心概念

| 概念 | 解决什么问题 | 本目录示例里怎么看到 |
|---|---|---|
| `healthcheck` + `depends_on.condition: service_healthy` | `depends_on` 默认只等容器"启动"，不等服务"可用"。不加健康检查，api 可能在 Postgres 还没接受连接时就起来并崩溃 | 示例 1 |
| `volumes` | 容器删了数据也不能丢（数据库文件放命名卷） | 示例 2 |
| `networks` / 服务名 DNS | 同一 compose 项目里服务名就是主机名，容器里连 `postgres:5432` 而不是 `localhost` | 示例 3 |
| `.env` 变量注入 | 密码/端口不写死在 yml，不进版本库 | 示例 4 |

## 示例 1：depends_on 不等于"就绪"

```bash
cd learning/docker-compose
docker compose -f demo-depends-on.yml up
```

`demo-depends-on.yml` 里 `slow-db` 故意延迟 8 秒才创建就绪标记文件，healthcheck 以此判断就绪。`client-naive` 只写 `depends_on: [slow-db]`，`client-healthy` 用 `condition: service_healthy`。观察日志：

- `client-naive` 立刻启动，检查就绪标记失败（模拟"连接被拒绝"）
- `client-healthy` 等到 8 秒后才启动，一次成功

```bash
docker compose -f demo-depends-on.yml down -v   # 清理
```

## 示例 2：卷持久化

```bash
docker compose -f demo-volumes.yml up -d
docker compose -f demo-volumes.yml exec writer sh -c 'echo "第一次写入 $(date)" >> /data/log.txt && cat /data/log.txt'
docker compose -f demo-volumes.yml down          # 删容器，不删卷
docker compose -f demo-volumes.yml up -d
docker compose -f demo-volumes.yml exec writer cat /data/log.txt   # 内容还在
docker compose -f demo-volumes.yml down -v       # -v 才会删卷（数据库丢数据的常见原因，慎用）
```

## 示例 3：服务名互通

仓库根目录起来 `docker compose up -d` 之后：

```bash
docker compose exec api python -c "import socket; print(socket.gethostbyname('postgres'), socket.gethostbyname('redis'))"
```

能解析出内网 IP，说明容器间是靠服务名 DNS 找到彼此的。再想一个问题：为什么 `DATABASE_URL` 里写 `postgres:5432`，而宿主机上跑 `learning/langgraph` 示例时写 `localhost:5432`？（答案：容器里的 localhost 是它自己；宿主机通过 `ports` 映射访问。）

## 示例 4：.env 注入与变量默认值

```bash
cp .env.template .env             # 仓库根目录
docker compose config             # 展示变量替换后的最终配置，排查"变量没生效"的第一手段
```

`${API_PORT:-8000}` 表示变量未设置时取默认值；没有默认值又没设置的变量，compose 会替换成空串并警告。

## 常用命令速查

```bash
docker compose up -d --build      # 后台启动并重新构建镜像
docker compose ps                 # 看各服务状态与 health
docker compose logs -f api        # 跟日志
docker compose exec postgres psql -U agentnexus   # 进数据库
docker compose down               # 停并删容器（保留卷）
docker compose down -v            # 连卷一起删
```
