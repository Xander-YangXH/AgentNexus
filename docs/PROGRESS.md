# 项目进度与架构总图

> 单一事实来源：**做到哪了、整体长什么样、接下来每一步是什么**。每完成一项就勾选，并同步 `HANDOFF.md`（交接）与 `dev-log.md`（过程与踩坑）。
> 图例：`[x]` 已完成并验证　`[ ]` 未做　`[~]` 已写但待你本机验证（说明见备注）

## 1. 当前位置

**阶段 4 进行中：4.1 独立 worker 与租约队列（0020）、4.2 工具 / 沙盒 / MCP / Skills（0021）已完成。下一步 4.3：多 Agent 编排与审批中断（先写选型草稿）。**

当前能做到的：创建用户 → 建对话 → 发消息，消息和 `queued` 的 run 同事务落库，随后被派发、抢占，由 LangGraph 聊天图执行（历史取自 messages 表，checkpoint 按 run 存放），助手回复落库，历史可查，用户间数据隔离；超时、异常、关停、重启都有收尾。
当前还能通过 `GET /api/runs/{id}/events` 实时收到逐段回复（SSE，可断线续传）。
打开 `http://localhost:8080` 即可在网页上使用。
生成中可以“停止”，失败或停止后可一键重试，失败原因以友好文案显示且刷新后仍在。
对话可改名、归档 / 恢复，列表按最近活动排序，长对话分页加载。
Agent 可以加载技能后在沙盒里执行 Python（pandas、matplotlib）、读写对话的工作目录、经 MCP 网关抓取网页（出口代理拦截内部地址）。
当前做不到的：还没有多 Agent 编排与审批中断，沙盒生成的文件还不能在网页上下载；没有真正的登录（`X-User-Id` 占位）。真实模型验证过 Gemini 与 DeepSeek（两种格式）。

### 完成度（由勾选框统计，粒度不等，仅作参考）

| 模块 | 已完成 | 待本机验证 | 总数 | 完成度 |
|---|---|---|---|---|
| M0 工程基础与环境 | 12 | 2 | 14 | 85% |
| M1 核心骨架与 Agent 引擎 | 53 | 0 | 53 | 100% |
| M2 RAG 知识库 | 12 | 0 | 12 | 100% |
| M3 知识图谱 | 7 | 0 | 8 | 87% |
| M4 多智能体/工具/沙盒/worker | 6 | 0 | 11 | 54% |
| M5 多租户与 Dashboard | 0 | 0 | 6 | 0% |
| M6 打磨与包装 | 0 | 0 | 6 | 0% |
| 贯穿：质量与文档 | 4 | 0 | 8 | 50% |
| **合计** | **94** | 2 | **118** | **79%** |

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
 ⑥ 执行：LangGraph 图运行，输入是 messages 表的历史，thread_id = run_id，checkpoint 存 Postgres（PostgresSaver）
        └ 工具调用 / 子 Agent 派发 / 审批中断(interrupt) / 沙盒执行
 ⑦ 流式事件：token 与步骤事件 → 事件通道 → ⑧
 ⑧ 客户端 GET /runs/{id}/events（SSE）订阅，断线可从游标续传
 ⑨ 完成：助手消息落库 + run=succeeded【同一事务】；失败 → failed + error
 ⑩ 阶段 4：worker 持租约(lease)+心跳；租约过期的 run 被他人接管，保证“不卡死、不重复执行”
```

标注：①~⑨ 已完成（单机版；Gemini 真实调用与流式已验证）；⑩ 租约未做（阶段 4）。完整走读见 `docs/guides/message-lifecycle.md`。

### 2.3 数据模型

已建（迁移 `0001`）：`users` → `conversations`（LangGraph `thread_id` 用 run_id）→ `messages`（`seq` 自增排序）；`agent_runs`（queued/running/succeeded/failed/cancelled，`output_message_id`、`error`、起止时间）。
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
| web | ✅ Nginx 提供打包页面并转发 `/api`（端口 8080） | 不变 |

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
- [x] 取消 run：`POST /api/runs/{id}/cancel`（排队中 200、执行中 202、已结束 409）— decisions/0009
- [x] 对话管理：改标题、归档 / 恢复、按最近活动排序、自动标题、消息游标分页（decisions/0012）
- [x] 统一错误格式 `{"error": {code, message, request_id}}`、request-id 中间件、结构化日志与 run 生命周期日志（decisions/0011）

**1.3 派发与执行** — decisions/0005
- [x] 写 decisions/0005：为什么先用单机 asyncio.Queue，而不是一开始就上 ARQ
- [x] 派发器 `RunDispatcher`：`submit` 成功后 `enqueue`；去重集合只是优化，正确性靠 `claim`
- [x] 补投递：启动时与周期扫描 `status=queued`（队列丢失/进程重启的兜底）
- [x] worker 循环：取任务 → `claim` → 执行 → `complete` / `fail`；异常写入 `error`
- [x] 超时（`RUN_TIMEOUT_SECONDS`）与并发上限（`WORKER_CONCURRENCY`）
- [x] 崩溃处理（单机版）：启动时把遗留 `running` 置 failed；优雅关停时在途 run 置 failed；写清单进程假设
- [x] 回声 Agent 打通整条链路 + 8 个测试 + 变异检查 + 容器内端到端
- [x] 取消 run：独立任务 + 先登记再取消 + `Task.cancelling()` 区分关停；重试 `POST /api/runs/{id}/retry`；错误码分类与数据迁移 0002/0003

**1.4 Agent 引擎** — decisions/0006
- [x] LLM 供应商适配（`models/`）：工厂 + 环境变量，`fake` / `openai`（OpenAI 兼容）/ `anthropic`；默认假模型
- [x] `agents/base.py`：`BaseAgent` / `BaseContext`
- [x] `agents/buildin/chatbot/`：`graph.py` / `state.py` / `prompt.py` / `context.py` 四件套 + `agent.py`
- [x] 调研并写 decisions/0006：历史以 messages 表为准，checkpoint 按 run 划分（含对照实验）
- [x] 0006 已确认（方案 A）
- [x] 接入 AsyncPostgresSaver：`thread_id = run_id`；checkpoint 表由 migrate 服务建
- [x] 消息映射（ChatMessage ↔ LangChain 消息）与测试；假模型图测试；变异检查
- [x] compose 传递 LLM / 执行器配置并在容器内验证
- [x] 新增 `google`（Gemini）provider + 冒烟脚本 `backend/scripts/check_llm.py`（假 key 验证了路径与错误处理）
- [x] **Gemini 真实调用已验证**（`gemini-3.8-flash`）：非流式/流式（长回答 12 个分块逐段到达）/完整服务路径两轮对话；首块延迟约 7.6s 需前端处理
- [x] openai 兼容与 anthropic 分支用真实 key 验证：用 DeepSeek 的两种兼容接口实测；默认 Anthropic 格式 + 关闭思考（decisions/0013）
- [x] checkpoint 清理：结束 24 小时后 `adelete_thread`，`checkpoint_purged_at` 记账 + 部分索引；实测平方增长（decisions/0010）

**1.5 流式输出** — decisions/0007（已实现）；学习导读 `docs/guides/message-lifecycle.md`
- [x] 调研并写 decisions/0007（含对照实验），用户确认方案 B 与“半截输出不入库”
- [x] 假模型补流式（`_astream`）
- [x] 事件契约 `events/types.py` 与 Redis Streams 发布/读取 `events/redis_stream.py`（`MAXLEN~`、终态 `EXPIRE`、发布失败不抛错）
- [x] 派发器：claim 后发 `run.started`；终态“先提交再发布”；启动恢复补发 `run.failed`
- [x] ChatbotAgent 改用 `astream(stream_mode=["messages","values"])`，逐块 emit token，最终回复取自最终状态
- [x] `GET /api/runs/{id}/events`（FastAPI 内置 SSE）：依赖里做归属校验、`Last-Event-ID` 续传、空闲查库并合成终态
- [x] 两个 Redis 客户端；阻塞读客户端 `socket_timeout` 大于阻塞时长（修复真实环境 bug）
- [x] 测试 19 个（服务层 + 真实 uvicorn 的 HTTP 层）+ 变异检查
- [x] 真实 Gemini 端到端验证：推送延迟 4~6ms，token 逐段到达
- [x] 产品层处理 LLM 高延迟：等待超过 20 秒提示可停止；失败/停止后一键重试

**1.6 前端 `web/`** — decisions/0008
- [x] Vue3 + Vite + Ant Design Vue 脚手架；开发时 Vite 代理、compose 里 Nginx 转发（同源）
- [x] SSE 客户端（fetch + 自写解析器 + `Last-Event-ID` 重连）与单测
- [x] 聊天页：发消息、逐段渲染、等待首字计时、失败显示“生成中断”、Markdown 渲染（禁用 HTML）
- [x] 对话列表与历史（刷新后仍在）；生成中刷新可接上（新增 `active-run` 接口）
- [x] 简易“用户选择”页（对应 `X-User-Id` 占位；新增 `GET /api/users`，阶段 5 删除）
- [x] compose 加入 `web` 服务（多阶段镜像，Nginx 1.28）
- [x] **里程碑：网页里和 LangGraph agent 聊天，流式回复，刷新后历史还在**（浏览器实测；界面上的 Gemini 两次遇到 503，成功路径用假模型验证，Gemini 的成功流式此前已在 API 层验证）
- [x] “停止生成”按钮
- [x] 失败/停止原因刷新后可见（`GET /api/conversations/{id}/runs`）；错误码翻译成友好文案，原始报错不对外

### M2 RAG 知识库（阶段 2）

- [x] 写 decision **草稿** 0014：本地 bge-small-zh embedding、bge-reranker 重排、Qdrant（派生索引）vs pgvector，含评测集与基准测试（`backend/eval/`）
- [x] 0014 已确认：本地 bge-small-zh-v1.5、默认开启重排、Qdrant（PostgreSQL 为事实来源）
- [x] compose 加入 qdrant（v1.19.2、数据卷、健康检查、关闭统计上报）；`storage/vectors.py`（单集合 + 租户索引 + 别名）；MinIO 暂缓（原件用 `BlobStore` + 本地数据卷，0015）
- [x] 表：knowledge_bases / documents / document_chunks（迁移 0006）；上传接口（校验 → 写原件 → 插记录，内容哈希去重，202）；原件存本地数据卷（decisions/0015）
- [x] 文档解析 `knowledge/parsing.py`：txt / md（UTF-8、GB18030）、pdf（pypdf，带页码）、docx（含表格）；失败按原因给错误码
- [x] 分块 `knowledge/chunking.py`：按中文句子边界、目标 300 / 上限 480 token、整句重叠；按 bge 分词器计数，并加字符下限防 `[UNK]` 击穿（2.2）
- [x] 入库流水线 `runtime/ingest.py`（先落库再投递、启动恢复改回 pending）：解析 → 分块（bge 分词器 + 字符下限）→ embedding（本地 bge-small-zh，构建时下载）→ 写 Qdrant → ready；失败有错误码，`reprocess` 重试；`knowledge.reindex` 从数据库重建索引（decisions/0016）
- [x] 检索 `services/search.py`：向量召回（按知识库过滤）→ 回库取原文（只要 ready、再按知识库过滤）→ bge-reranker 重排（默认开，候选可调）；`POST /api/knowledge/search`；端到端评测 R@1 0.90 → 1.00（decisions/0017）
- [x] 知识库页面：知识库管理、上传、状态轮询、失败原因与重新处理、查看分块、检索测试（来源 / 两种分数 / 各步耗时）
- [x] 知识库问答（decisions/0018）：对话关联知识库（范围由服务端定）、预检索 + `knowledge_search` 工具（最多 3 轮）、引用快照存 `messages.citations`、`tool.started/finished` 事件、前端来源与原文
- [x] RAG 三层评测：模型（0014）、检索系统 `eval_search_api.py` / `eval_chunking_md.py`、问答 `eval_answer_api.py`（预检索 30/30，模型调用减半，每轮约 $0.008）；结果在 `backend/eval/results/`
- [x] **里程碑：上传文档后 Agent 基于检索内容回答，并展示引用片段**（2026-10-22 浏览器 + 真实 DeepSeek 验证）

### M3 知识图谱（阶段 3）

- [x] compose 加入 neo4j（2026.08.1-community，堆 512m / 页缓存 256m，实际约 1GB）；`knowledge/graph/store.py`（异步驱动）；就绪探针加 neo4j
- [x] 抽取 `knowledge/graph/extraction.py`：结构化输出（强制工具调用 + Pydantic），解析失败重试一次；提示词 v2（实体上限、数值和代码标识符不成为节点、谓词清单）；规则版假抽取器
- [x] 构建 `services/graph.py`：按内容去重 → 缓存 → 并发抽取 → 合并（`merge.py`）→ PostgreSQL（事实来源）→ 同步 Neo4j；预估费用、上限、一个知识库同时只能有一个构建、启动恢复；`graph.sync` 重建命令
- [x] 抽取评估 `eval_kg_extract.py`：两份语料 v1 / v2，抽样核对关系正确率约 76~78%，v2 费用 -28%（decisions/0019）
- [x] 图查询 API：概览、搜索、一跳邻居、最短路径（Neo4j）；实体详情与出处原文（PostgreSQL）
- [x] 前端图谱浏览（cytoscape.js）：构建栏（预估 / 确认 / 进度 / 实际费用）、搜索定位、详情与出处、展开邻居、最短路径高亮
- [ ] （可选）图谱参与检索：图扩展召回
- [x] **里程碑：选一个知识库，看到自动抽取的实体关系图并可展开**（2026-10-22，5 篇决策记录 → 368 个实体 / 381 条关系，$0.05）

### M4 多智能体、工具与扩展生态（阶段 4，最重）

- [x] **独立 worker 容器 + PostgreSQL 租约队列**（实验后替代 ARQ，decisions/0020）：`worker.py`、`runtime/lease.py`；api 只提交（事务内 NOTIFY），worker 执行 run / 入库 / 图谱构建 / checkpoint 清理；可多副本
- [x] **租约 / 心跳 / fencing**：迁移 0009（三张表加租约字段 + 取消标记）；SKIP LOCKED 认领、10 秒心跳、版本号校验写回；run 从 checkpoint 续做（最多 3 次）；跨进程取消（数据库标记 + Redis 广播）
- [x] 崩溃恢复测试：compose 2 副本，kill -9 后 24.8 秒接管、docker stop 后 0.9 秒接管，均只有 1 条回复；测试覆盖过期接管、fencing、多 worker 恰好一次
- [ ] `agents/middlewares/`：subagent 派发、摘要压缩、审批中断（interrupt + resume）
- [ ] 多 Agent 编排：supervisor 派发 + 子 Agent 并行执行 + 汇总
- [ ] “深度调研”Agent：拆解任务 → 并行子任务 → 汇总成可下载文件（产物进 MinIO）
- [x] 沙盒（decisions/0021）：独立 sandbox 服务独占 Docker socket（`sandbox/`），每次执行新建加固容器（断网、只读、nobody、进程 / 内存 / CPU 上限、超时）；工作空间按对话划分（卷子目录挂载 + 路径校验）；威胁模型见 0021；工具 `python_exec` / `list_files` / `read_file` / `write_file`
- [x] MCP：网关（`mcp_gateway/`，只接入 internal 网络、每次调用新起子进程）接入官方 fetch 服务，Squid 出口代理按解析后的 IP 拒绝内部地址；worker 直接用 mcp SDK（实验后未用 langchain-mcp-adapters）
- [x] Skills：`backend/skills/*.md`（data-analysis、web-research）+ `load_skill`；工具只在加载技能后启用，执行前再检查；真实 DeepSeek 跑通数据分析与网页问答
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
- [x] 后端依赖锁文件 `backend/requirements.lock`（uv `--exclude-newer`：含间接依赖只取发布满两周的版本，decisions/0016）
- [x] 日志与可观测性：结构化日志（text/json）、request-id 与 run_id 贯穿（decisions/0011）
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
