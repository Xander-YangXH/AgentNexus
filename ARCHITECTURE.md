# AgentNexus 架构

> 这份文档帮你回答：系统由哪几个进程组成、一个请求怎么流过它们、代码在哪里、哪些规则不能破坏。
> 每个设计的来龙去脉（为什么这样、否掉了什么、实验数据）在 `docs/decisions/`；按主题的走读在 `docs/guides/`。

## 1. 鸟瞰

AgentNexus 是一个企业知识库与 Agent 平台：用户把文档放进知识库，和 Agent 对话（回答逐句标注引用）、浏览从文档抽取的知识图谱、发起“深度调研”（确认计划后并行调研、生成报告）。Agent 可以在隔离沙盒里执行代码、经出口代理抓取网页。多用户按部门隔离，用量可统计。

```
                          浏览器（Vue 3 + Ant Design Vue）
                                      │  同源 /api（HttpOnly Cookie 会话）
                                      ▼
┌──────────┐   /api    ┌──────────────────────┐  NOTIFY / 落库   ┌──────────────────────────────┐
│ web      │──────────▶│ api（FastAPI）        │────────────────▶│ worker（可多副本）             │
│ Nginx    │           │ 收请求、鉴权、落库、    │                 │ 对话 / 调研 run、文档入库、    │
│ 静态文件  │◀── SSE ───│ 读事件流              │◀── Redis 事件 ──│ 图谱构建、checkpoint 清理      │
└──────────┘           └──────────┬───────────┘                 └───┬──────────┬───────────┬───┘
                                  │                                 │ HTTP+令牌 │ MCP/HTTP  │ LLM API
                ┌─────────────────┼─────────────────┐               ▼          ▼           ▼
                ▼                 ▼                 ▼        ┌──────────┐ ┌───────────┐  外部模型
          PostgreSQL           Redis            Qdrant       │ sandbox  │ │mcp-gateway│
     （事实来源：业务数据、  （事件流、取消广播）  （向量索引，  │ 独占 Docker│ │第三方 MCP  │
       任务租约、checkpoint、                    可重建）     │ socket    │ │服务子进程  │
       用量；RLS 隔离）        Neo4j（图查询，可从 PG 重建）    └────┬─────┘ └─────┬─────┘
                                                                    ▼             ▼
                                                           一次性加固容器    egress-proxy（Squid）
                                                           （断网、只读、     → 外网 80/443，
                                                             nobody）         拒绝内网地址
```

| 进程 / 服务 | 职责 | 数据库角色 |
|---|---|---|
| web | Nginx：前端静态文件；`/api` 同源转发（不需要 CORS） | — |
| api | 接收请求、登录与鉴权、落库并在同一事务发 NOTIFY、SSE 读事件流。不执行后台任务 | `agentnexus_api`（受 RLS 约束） |
| worker | 认领并执行任务（租约 + 心跳 + fencing）；可 `--scale worker=N` | `agentnexus_worker`（BYPASSRLS） |
| migrate | 一次性：Alembic 迁移 → checkpoint 表 → 创建 / 更新数据库角色并授权 | 表所有者 |
| sandbox | 唯一持有 Docker socket；只开放“在某个工作空间里执行代码 / 读写文件” | — |
| mcp-gateway | 以子进程运行第三方 MCP 服务（每次调用一个新进程），只接入 internal 网络 | — |
| egress-proxy | Squid：按解析后的 IP 拒绝私有 / 回环 / 链路本地地址 | — |
| PostgreSQL / Redis / Qdrant / Neo4j | 见第 4 节“数据放在哪里” | — |

## 2. 代码地图

```
backend/
├── server/                     接口层（薄）：路由、请求 / 响应模型、错误映射、中间件
│   ├── main.py                 应用工厂与 lifespan（装配见 agentnexus/assembly.py）
│   ├── deps.py                 依赖：当前用户（Cookie → 会话）、服务实例、归属校验
│   ├── csrf.py / middleware.py CSRF 检查；request-id 与访问日志
│   └── routers/                conversations、events（SSE）、knowledge、graph、artifacts、usage、users
├── package/agentnexus/         业务逻辑（厚）
│   ├── assembly.py             api / worker 共用的装配（模型、存储、服务、派发器）
│   ├── worker.py               worker 进程入口：LISTEN 唤醒、存活文件、优雅关停
│   ├── cli.py                  管理命令：创建用户、设置密码、建部门
│   ├── agents/
│   │   ├── buildin/chatbot/    聊天图：预检索 → 模型 →（工具 → 模型）*，引用快照，技能启用工具
│   │   ├── buildin/research/   调研图：计划 → 审批（interrupt）→ Send 并行子 Agent → 汇总报告
│   │   ├── tools/              通用工具层：沙盒工具、MCP 工具（经网关）
│   │   └── skills.py           Skills：Markdown 文件头 + 正文，load_skill 后才启用工具
│   ├── runtime/                lease.py（租约队列）、dispatcher.py（run 派发、取消、审批、超时）、
│   │                           jobs.py（入库 / 图谱任务）、cancel_bus.py、janitor.py（checkpoint 清理、审批过期）
│   ├── services/               用例层：runs、conversations、knowledge、search、graph、artifacts、usage、run_events
│   ├── repositories/           数据访问（SQLAlchemy）
│   ├── knowledge/              解析（PDF / Word 在子进程里，isolation）、分块、向量化、重排、图谱抽取与合并、Neo4j 存储
│   ├── security/               登录与会话（auth）、登录限流（throttle）、可见性（access）、RLS 身份（principal）
│   ├── sandbox/                sandbox 服务（app、runner、files）与 worker 侧客户端
│   ├── mcp_gateway/            MCP 网关（只依赖 mcp / starlette，单独打镜像）
│   ├── storage/                PostgreSQL 模型与会话、原件存储、Qdrant 索引、checkpoint、数据库角色
│   ├── events/ models/ config/ observability/   事件契约、模型工厂（含假模型）、配置、结构化日志
├── migrations/                 Alembic（只往前加）
├── skills/                     技能文件（data-analysis、web-research）
├── tests/                      集成测试：真实 PostgreSQL / Redis / Neo4j，沙盒测试用本机 Docker
└── eval/                       选型实验与评测脚本（每个决策的数据来源）
web/src/
├── api/                        http（统一错误、401 回登录页）、sse（fetch 读 SSE、断线续传）、types
├── composables/                纯函数状态机与逻辑：pendingReply（流式 / 审批 / 接管）、useChat、usage……
├── components/                 页面：ChatPanel、ConversationList、KnowledgePanel、GraphPanel、UsagePanel、AdminPanel、LoginPanel、NavRail
├── theme/ styles/              设计 token（浅色 / 深色）与全局样式
docker/                         各镜像的 Dockerfile、nginx.conf、squid.conf、沙盒与网关的锁文件
docs/                           decisions（ADR）、guides（走读）、dev-log、interview-prep、PROGRESS、HANDOFF
```

## 3. 核心链路

### 3.1 一条消息（`docs/guides/message-lifecycle.md`）
1. `POST /conversations/{id}/messages`：用户消息 + `queued` 的 run + 一行用量，**同一事务**写入，事务里 `pg_notify`。
2. worker 被唤醒（或轮询兜底），`SKIP LOCKED` 认领：状态改为 `running`，租约版本号 +1。
3. 按对话的 Agent 类型执行图；token、工具调用等事件写进 Redis Stream；心跳每 10 秒续约，同时检查取消标记。
4. 完成：助手消息、引用快照、产物元数据与“run 成功”在**同一事务**里写入，条件带租约版本号；然后发布终态事件。
5. 浏览器用 fetch 读 SSE（带 Last-Event-ID 续传）；事件只是视图，流结束后以数据库为准刷新。

### 3.2 文档入库与检索（`docs/guides/rag-pipeline.md`）
上传 → 原件写入存储 → `pending` 文档（同事务 NOTIFY）→ worker：解析 → 按句分块 → 本地 embedding → 写 Qdrant → 分块与 `ready` 同事务落库。
检索：向量召回 → 回数据库取原文（只取 `ready` 文档、按可见知识库过滤）→ 重排。

### 3.3 深度调研（`docs/guides/multi-agent-research.md`）
计划（结构化输出）→ `interrupt` 等审批：run 改为 `awaiting_approval`、**放弃租约**、SSE 暂停 → 用户答复后回到 `queued` → 任意 worker 从 checkpoint 继续 → `Send` 并行子 Agent（崩溃只重做未完成的）→ 汇总报告存为产物。

### 3.4 工具调用（`docs/guides/tools-sandbox.md`）
模型只看到已加载技能启用的工具；执行前再检查。代码执行：worker → sandbox 服务 → 新建加固容器（工作空间按对话、卷子目录挂载）。抓取：worker → mcp-gateway → 子进程 → Squid → 外网。

## 4. 数据放在哪里
| 数据 | 存储 | 说明 |
|---|---|---|
| 用户、对话、消息、run、知识库、文档、分块、图谱、产物元数据、用量 | PostgreSQL | **事实来源**；租约字段就在业务表上 |
| 对话执行中的状态 | PostgreSQL（LangGraph checkpoint） | thread_id = run_id；结束 24 小时后清理 |
| 实时事件（token、工具、进度） | Redis Streams | 只是视图；终态后保留 1 小时 |
| 向量 | Qdrant | 派生索引，可用 `knowledge.reindex` 重建 |
| 图查询 | Neo4j | 派生存储，可用 `knowledge.graph.sync` 重建 |
| 文档原件、报告 | 数据卷（原件存储，接口与对象存储一致） | 按 key 存取，元数据在 PostgreSQL |
| 沙盒文件 | 数据卷，按对话分子目录 | 只有 sandbox 服务访问 |

## 5. 架构不变量（改代码前先读）
1. **数据库是事实来源**：任务以落库的记录为准，通知、事件、缓存、向量、图都只是派生物；派生物丢了可以重建，反之不行。
2. **写回带租约版本号**：worker 对 run / 文档 / 图谱构建的所有写回都要带 `lease_version` 条件；被接管的旧执行者一定写不进去。外部系统（Qdrant）不认版本号：只删自己写的，只由赢家清场。
3. **同一对话同时最多一个活跃 run**（排队、执行、等待审批都算）：由部分唯一索引保证，避免两个 run 写同一个 checkpoint。
4. **“在哪执行、以谁的身份”由服务端决定**：检索哪些知识库、工作空间是哪个对话，都从 run 的归属推出，不出现在模型可控的工具参数里。
5. **两道隔离**：服务层显式检查归属 / 可见性（返回 404 / 403）；api 进程以受 RLS 约束的角色连接，身份在每个事务开始时用**事务级**设置写入。不要让 api 用超级用户或表所有者连接。
6. **有成本或副作用的步骤不和 `interrupt()` 放在同一个节点**：恢复时含 interrupt 的节点会从头执行。
7. **只有 sandbox 服务持有 Docker socket**；第三方工具代码只在 mcp-gateway 里运行，且只能经出口代理出网。
8. **迁移只往前加**；新表要等 migrate 服务执行 `storage/roles.py` 后 api / worker 才有权限。
9. **前端不保存业务状态**：流式、审批、接管的状态在纯函数状态机里，终态以接口返回的数据库状态为准；样式只用设计 token。
10. **安全检查出错时拒绝，不放行**：登录限流计不了数返回 503；不可信文件的解析在子进程里，超时或超内存就判失败。

## 6. 横切关注点
- **认证**：服务端会话（会话表存令牌哈希）+ HttpOnly / SameSite=Lax Cookie；写请求校验 Origin。密码 argon2id，在线程池里计算。登录限流按“账号 + IP”和按 IP 计数（Redis），客户端 IP 取 Nginx 覆盖的 `X-Real-IP`。威胁模型与已知局限见 `SECURITY.md`。
- **可观测性**：每个请求一个 request-id，run 执行期间日志自动带 run_id；结构化日志（text / json）。用量从回调采集，写 `usage_events`。
- **配置**：全部来自环境变量（`config/settings.py`），compose 通过 `.env` 注入；密钥只在需要它的服务里出现。
- **依赖**：后端按 `requirements.lock`（uv，只取发布满两周的版本；有已知漏洞且影响我们的包例外）安装；前端 `package-lock.json`。CI 每次用 pip-audit / npm audit 扫描，豁免写在 `scripts/audit-allowlist.json`（带到期日）。
- **测试**：后端集成测试用真实数据库和 Docker，模型一律用假模型；越权矩阵用两种数据库角色各跑一遍；安全相关逻辑做变异检查。CI 见 `.github/workflows/ci.yml`。

## 7. 继续阅读
- 决策记录索引：`docs/interview-prep.md` 的“架构决策记录索引”，或直接看 `docs/decisions/`。
- 按主题的走读（每篇末尾有自测题）：`docs/guides/`。
- 开发过程、踩坑与问题索引：`docs/dev-log.md`。
