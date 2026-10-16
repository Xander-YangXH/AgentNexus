# 项目进度与架构总图

> 单一事实来源：**做到哪了、整体长什么样、接下来每一步是什么**。每完成一项就勾选，并同步 `HANDOFF.md`（交接）与 `dev-log.md`（过程与踩坑）。
> 图例：`[x]` 已完成并验证　`[ ]` 未做　`[~]` 已写但待你本机验证（说明见备注）

## 1. 当前位置

**阶段 1「核心骨架」进行中：数据层、API 层、派发与执行（回声 Agent）已完成，下一步是 1.4「接入 LangGraph chatbot + PostgresSaver」。**

当前能做到的：创建用户 → 建对话 → 发消息，消息和 `queued` 的 run 同事务落库，随后被派发、抢占、执行（回声 Agent），助手回复落库，历史可查，用户间数据隔离；超时、异常、关停、重启都有收尾。
当前做不到的：没有 LLM（回复只是回声）、没有流式输出、没有前端页面、不能取消 run。

### 完成度（由勾选框统计，粒度不等，仅作参考）

| 模块 | 已完成 | 待本机验证 | 总数 | 完成度 |
|---|---|---|---|---|
| M0 工程基础与环境 | 12 | 2 | 14 | 85% |
| M1 核心骨架与 Agent 引擎 | 18 | 0 | 37 | 48% |
| M2 RAG 知识库 | 0 | 0 | 11 | 0% |
| M3 知识图谱 | 0 | 0 | 8 | 0% |
| M4 多智能体/工具/沙盒/worker | 0 | 0 | 11 | 0% |
| M5 多租户与 Dashboard | 0 | 0 | 6 | 0% |
| M6 打磨与包装 | 0 | 0 | 6 | 0% |
| 贯穿：质量与文档 | 2 | 0 | 7 | 28% |
| **合计** | **32** | 2 | **100** | **32%** |

> 任务数不等于工作量：M4 的单项远重于 M0。

## 2. 架构总览

### 2.1 目标形态（分层）

```
┌─────────────────────────── web/ (Vue3 + Vite + Ant Design Vue) ───────────────────────────┐
│  聊天页（SSE 流式）  知识库管理/检索测试  图谱浏览  Agent/工具配置  Dashboard  登录/权限     │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │ HTTP / SSE
┌───────────────────────────────────────────▼───────────────────────────────────────────────┐
│ backend/server  —— FastAPI（薄）：路由 · 依赖注入 · 鉴权 · 请求/响应 schema · 领域异常→状态码  │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │ 调用
┌───────────────────────────────────────────▼───────────────────────────────────────────────┐
│ backend/package/agentnexus  —— 业务包（厚）                                                  │
│   services/      用例层：一个方法一个事务，承载业务规则                                       │
│   repositories/  持久化访问：单表读写，不管事务                                               │
│   agents/        BaseAgent · buildin/chatbot（graph/state/prompt/context）· middlewares     │
│                  · backends/sandbox · toolkits（含 MCP、knowledge_search）                   │
│   knowledge/     parser · chunking · embedding/rerank · implementations/qdrant · graphs     │
│   models/        LLM 供应商适配                                                              │
│   storage/       postgres / redis / minio / neo4j / qdrant 连接管理                         │
│   workspace/     用户工作区与沙盒文件路径       config/  系统与用户配置                       │
└───────┬───────────────┬───────────────┬───────────────┬───────────────┬───────────────────┘
        ▼               ▼               ▼               ▼               ▼
   PostgreSQL         Redis           Qdrant          MinIO           Neo4j
 业务事实+checkpoint  队列信号/事件流   向量检索       文档与产物       知识图谱
```

### 2.2 主链路（Agent 运行）——整个项目的核心

```
 ① 客户端 POST /conversations/{id}/messages
 ② 服务层【同一事务】写入：用户消息 + AgentRun(status=queued)        ← 事实来源是数据库
 ③ 返回 202 + run               （提交成功 ⇒ 任务一定会被执行）
 ④ 派发：把 run_id 投给队列      （单机 asyncio.Queue → 阶段 4 换 ARQ + 独立 worker）
        └ 兜底：启动时/定期扫描 status=queued 的 run 补投递（队列丢了也不丢任务）
 ⑤ worker 抢占：UPDATE … WHERE status='queued'，rowcount=1 者执行   ← 并发安全交给数据库
 ⑥ 执行：LangGraph 图运行，thread_id = conversation_id，checkpoint 存 Postgres（PostgresSaver）
        └ 工具调用 / 子 Agent 派发 / 审批中断(interrupt) / 沙盒执行
 ⑦ 流式事件：token 与步骤事件 → 事件通道 → ⑧
 ⑧ 客户端 GET /runs/{id}/events（SSE）订阅，断线可从游标续传
 ⑨ 完成：助手消息落库 + run=succeeded【同一事务】；失败 → failed + error
 ⑩ 阶段 4：worker 持租约(lease)+心跳；租约过期的 run 被他人接管，保证“不卡死、不重复执行”
```

标注：①②③④⑤⑨ 已完成（单机版，⑥ 目前是回声 Agent）；⑥ 的 LangGraph 部分、⑦⑧ 流式、⑩ 租约未做。

### 2.3 数据模型

已建（迁移 `0001`）：`users` → `conversations`（id 即 LangGraph `thread_id`）→ `messages`（`seq` 自增排序）；`agent_runs`（queued/running/succeeded/failed/cancelled，`output_message_id`、`error`、起止时间）。
计划追加：`agent_runs` 的 lease 字段（阶段 4）；`knowledge_bases` / `documents` / `chunks`（阶段 2）；实体与关系的图谱元数据（阶段 3）；`departments`、角色、资源可见性、`usage_records`（阶段 5）。

### 2.4 架构不变量（改代码时不能破坏）

1. 数据库是事实来源，队列只是触发器；“消息 + queued run” 必须同事务。
2. 并发正确性靠数据库：条件更新抢占、部分唯一索引（同一对话最多一个活跃 run）、终态不可覆盖。
3. 路由薄、服务厚、仓储不管事务；服务只抛领域异常，HTTP 状态码只在入口映射。
4. 越权与不存在同样返回 404，不泄露资源存在性。
5. 配置只从环境变量读取，不进版本库；迁移由一次性 `migrate` 服务执行，不在 api 启动时跑。

### 2.5 部署拓扑

| 服务 | 现状 | 目标 |
|---|---|---|
| postgres / redis | ✅ 运行，带 healthcheck 与数据卷 | 不变 |
| migrate（一次性） | ✅ 运行，api 等其成功退出 | 不变 |
| api | ✅ 运行 | 不变 |
| worker | ✅ 与 api 同进程（单实例假设） | 阶段 4 拆成独立容器 + lease |
| qdrant / minio / neo4j | ⬜ | 阶段 2 / 2 / 3 引入 |
| web | ⬜ | 阶段 1 起，阶段 6 纳入 compose |

---

## 3. 任务清单

### M0 工程基础与环境（阶段 0）

**学习**
- [x] LangGraph：StateGraph / checkpoint / interrupt（`learning/langgraph/`）
- [~] LangGraph 示例 2–4 在本机跑通（示例 1 已跑通；2–4 需要 Postgres，现在 compose 里已有，可连 `localhost:5432`，注意改 `PG_URI` 的账号密码）
- [x] FastAPI：依赖注入 / BackgroundTasks / SSE / async 陷阱（`learning/fastapi/`）
- [x] Docker Compose：healthcheck、卷、服务名 DNS、`.env`（`learning/docker-compose/`）
- [~] 自己跑一遍 `learning/docker-compose/` 的两个 demo

**选型与文档**
- [x] 技术栈基线与取舍（decisions/0001、tech-tradeoffs.md）
- [x] 不引入 LlamaIndex（decisions/0002）
- [x] Compose + healthcheck（decisions/0003）
- [x] monorepo 目录骨架、`.gitignore`、`.dockerignore`

**环境交付物**
- [x] `.env.template`
- [x] `docker-compose.yml`：postgres + redis + migrate + api，healthcheck 与 `depends_on` 条件
- [x] `docker/api.Dockerfile`（依赖层缓存、监听 0.0.0.0）
- [x] `/health`（存活）与 `/health/ready`（就绪，查 Postgres/Redis）
- [x] **里程碑：`docker compose up` 后 `/health` 返回 200**（含停 postgres 的故障用例）

### M1 核心骨架与 Agent 编排引擎（阶段 1）

**1.1 数据层** — decisions/0004
- [x] 选型：SQLAlchemy 2.0 异步 + psycopg3 + Alembic
- [x] 表：users / conversations / messages / agent_runs（CHECK 约束、`seq` 排序、UUID 主键）
- [x] 不变量：同一对话最多一个活跃 run（部分唯一索引）
- [x] 迁移 `0001` + compose 一次性 `migrate` 服务
- [x] 仓储层：users / conversations / messages / runs（含原子 `claim`、`finish`、`list_queued`）
- [x] 服务层：`submit`（消息+run 同事务）、`claim`、`complete`、`fail`、`get`（带归属校验）
- [x] 集成测试（真实 Postgres）：原子提交、回滚无孤儿、8 路并发 claim 仅 1 成功、终态不可覆盖、消息顺序

**1.2 API 层**
- [x] 用户 / 对话 / 消息 / run 的 REST 接口；发消息返回 202
- [x] 鉴权占位 `X-User-Id`（阶段 5 替换 `current_user_id`）
- [x] 领域异常 → 状态码统一映射（404/409/401）
- [x] API 测试：隔离、冲突、校验；compose 内 curl 端到端
- [ ] 取消 run：`POST /runs/{id}/cancel`（queued 直接置 cancelled；running 发取消信号）
- [ ] 对话管理补全：改标题、删除/归档；消息分页
- [ ] 全局错误响应结构统一（错误码 + 信息），请求日志与 request-id

**1.3 派发与执行** — decisions/0005
- [x] 写 decisions/0005：为什么先用单机 asyncio.Queue，而不是一开始就上 ARQ
- [x] 派发器 `RunDispatcher`：`submit` 成功后 `enqueue`；去重集合只是优化，正确性靠 `claim`
- [x] 补投递：启动时与周期扫描 `status=queued`（队列丢失/进程重启的兜底）
- [x] worker 循环：取任务 → `claim` → 执行 → `complete` / `fail`；异常写入 `error`
- [x] 超时（`RUN_TIMEOUT_SECONDS`）与并发上限（`WORKER_CONCURRENCY`）
- [x] 崩溃处理（单机版）：启动时把遗留 `running` 置 failed；优雅关停时在途 run 置 failed；写清单进程假设
- [x] 回声 Agent 打通整条链路 + 8 个测试 + 变异检查 + 容器内端到端
- [ ] 取消 run（与 1.2 的取消接口一起做）

**1.4 Agent 引擎**
- [ ] LLM 供应商适配（`models/`）：配置项进 `.env.template`，统一接口，便于换模型
- [ ] `agents/base.py`：`BaseAgent` / `BaseContext`
- [ ] `agents/buildin/chatbot/`：`graph.py` / `state.py` / `prompt.py` / `context.py` 四件套
- [ ] 接入 PostgresSaver：`thread_id = conversation_id`，checkpoint 与业务表同库
- [x] 调研并写 decisions/0006 **草稿**：历史以 messages 表为准，checkpoint 按 run 划分（含 7 条对照实验结论）
- [ ] **等你确认 0006**，并选定 LLM 供应商
- [ ] 实现：消息映射（ChatMessage ↔ LangChain 消息）与测试
- [ ] 单元测试：图用假 LLM 跑通；真实 LLM 仅手动验证

**1.5 流式输出**
- [ ] 事件模型：token / 步骤 / 完成 / 错误，带单调递增游标
- [ ] 事件通道（先进程内，是否用 Redis Stream 在 0005 或新 decision 里定）
- [ ] `GET /runs/{id}/events`（SSE）：断线可凭游标续传
- [ ] 测试：流式顺序正确、客户端断开后服务端释放资源、run 结束后重连能收到终态

**1.6 前端 `web/`**
- [ ] Vue3 + Vite + Ant Design Vue 脚手架，接入 compose / 代理
- [ ] 聊天页：发消息、流式渲染、停止按钮
- [ ] 对话列表与历史（刷新后仍在）
- [ ] 简易“用户选择”页（对应 `X-User-Id` 占位）
- [ ] **里程碑：网页里和基础 LangGraph agent 聊天，流式回复，刷新后历史还在**

### M2 RAG 知识库（阶段 2）

- [ ] 写 decision：向量库选型（Qdrant）与 embedding / rerank 模型接入方式
- [ ] compose 加入 qdrant、minio；`storage/` 增加对应连接管理
- [ ] 表：knowledge_bases / documents / chunks；文档上传落 MinIO，元数据落 Postgres
- [ ] 文档解析 `knowledge/parser`：PDF / Word / Markdown（pypdf、python-docx、unstructured 三选一起步，写选型理由）
- [ ] 分块 `knowledge/chunking`：段落/语义边界切分 + token 统计；重叠与大小可配置
- [ ] 入库流水线（异步任务，复用 AgentRun 式的“先落库再投递”）：解析 → 分块 → embedding → 写 Qdrant，状态可查、失败可重试
- [ ] 检索：向量召回 + 标量过滤，（可选）rerank
- [ ] 检索测试页面：输入 query，看召回片段与分数
- [ ] Agent 工具 `knowledge_search`，回答附引用片段
- [ ] RAG 评估集（小而真实）与指标：命中率 / 引用正确率，用来支撑面试里的数字
- [ ] **里程碑：上传文档后 Agent 基于检索内容回答，并展示引用片段**

### M3 知识图谱（阶段 3）

- [ ] compose 加入 neo4j；`storage/` 连接管理
- [ ] 抽取 prompt：让 LLM 输出结构化实体/关系 JSON，带校验与重试
- [ ] `knowledge/graphs`：从已入库 chunk 抽取 → 去重/合并实体 → 写 Neo4j
- [ ] 抽取的准确率与成本评估（抽样人工核对）
- [ ] 图查询 API：关键词搜实体、取邻居子图
- [ ] 前端图谱浏览：搜实体、点节点看属性、高亮关联子图
- [ ] （可选）图谱参与检索：图扩展召回
- [ ] **里程碑：选一个知识库，看到自动抽取的实体关系图并可展开**

### M4 多智能体、工具与扩展生态（阶段 4，最重）

- [ ] **ARQ + 独立 worker 容器**：把 1.3 的内存队列升级，worker 拆出 compose
- [ ] **lease / heartbeat**：`agent_runs` 追加租约字段（新迁移）；worker 续租；过期可被接管；防重复执行
- [ ] 崩溃恢复测试：杀掉 worker 再重启，未完成任务被接管，既不卡死也不重复
- [ ] `agents/middlewares/`：subagent 派发、摘要压缩、审批中断（interrupt + resume）
- [ ] 多 Agent 编排：supervisor 派发 + 子 Agent 并行执行 + 汇总
- [ ] “深度调研”Agent：拆解任务 → 并行子任务 → 汇总成可下载文件（产物进 MinIO）
- [ ] 沙盒：`agents/backends/sandbox/`，Docker SDK 起隔离容器（资源限制、网络隔离、超时、文件挂载边界）；明确威胁模型
- [ ] 接入至少一个 MCP server（`langchain-mcp-adapters`）
- [ ] Skills：简化为“按需加载的工具配置”
- [ ] 审批流 UI：中断后用户批准/拒绝再恢复
- [ ] **里程碑：复杂任务被拆解并行执行并产出可下载报告；杀 worker 后任务被正确接管**

### M5 多租户权限与 Dashboard（阶段 5）

- [ ] RBAC 模型：用户 / 部门 / 角色；资源可见性（Agent、知识库）
- [ ] 真实认证：登录 + token，替换 `current_user_id`（路由与服务层不动）
- [ ] 权限校验用依赖注入强制执行在后端；补越权测试矩阵
- [ ] 用量采集：请求量、token 消耗、检索次数（`usage_records`）
- [ ] Dashboard：请求量 / Token / 检索频次图表
- [ ] **里程碑：两个角色账号互相看不到对方数据；Dashboard 显示真实用量曲线**

### M6 打磨、部署与简历包装（阶段 6）

- [ ] 完整 `docker-compose.yml` 一键起全部服务；从零 `git clone` + `up` 实测
- [ ] `ARCHITECTURE.md`：鸟瞰、代码地图、核心链路、不变量（本文件 §2 是素材）
- [ ] 客服工单场景包端到端 + Agent 并行任务场景
- [ ] README 截图、3–5 分钟演示视频
- [ ] 面试资产定稿：各模块问答、排错故事、量化数据
- [ ] 推送到 GitHub（目前提交均只在本地）

### 贯穿全程：质量与文档

- [x] 约定：每步写 dev-log（选型表 + 问题索引）并同步 interview-prep（AGENTS.md 第 6 条）
- [x] 集成测试跑真实 Postgres
- [ ] CI（GitHub Actions）：起 Postgres 服务跑测试 + 构建镜像
- [ ] lint / 格式化 / 类型检查（ruff、mypy 或 pyright）
- [ ] 日志与可观测性：结构化日志、run 级 trace id
- [ ] 安全清单：密钥管理、依赖扫描、沙盒逃逸面
- [ ] 每个阶段结束回填 interview-prep 的占位小节

---

## 4. 需要你本机确认的项

1. LangGraph 示例 2–4（现在可以直接连 compose 里的 Postgres）。
2. `learning/docker-compose/` 的两个 demo。
3. 把本地提交推送到 GitHub（我不会擅自推送）。

## 5. 已知风险与待决问题

- **事实来源重复**：历史消息既在 `messages` 表又会进 LangGraph checkpoint，要在 1.4 明确以谁为准。
- **单机队列的局限**：进程崩溃会丢内存队列，靠“扫描 queued”兜底；`running` 状态遗留的 run 在单机阶段如何处理需要明确取舍。
- **Python 版本**：本机 3.13，容器 3.12，目前都兼容，依赖升级时留意。
- **阶段 4 体量大**：工期紧时按 ROADMAP 的砍项顺序取舍，沙盒隔离不建议砍。
