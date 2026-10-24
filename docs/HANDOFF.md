# 交接说明（当前进度）

> 供新会话（如 Claude Code）快速接上上下文。每完成一个里程碑就更新本文件。

> 完整进度、架构总图与分模块任务清单见 [PROGRESS.md](PROGRESS.md)；本文件只保留最近的交接要点。

## 项目
AgentNexus：企业级多租户知识库与 Agent 平台（RAG、知识图谱、LangGraph 多 Agent、MCP/Skills、沙盒、多租户、仪表盘），用作 2026-2027 大模型算法 / Agent 开发求职项目。远程仓库：https://github.com/Xander-YangXH/AgentNexus

## 当前阶段：ROADMAP 阶段 6「打磨、部署与简历包装」——基本完成，只剩演示视频（用户录制）
- [x] LangGraph 学习：`learning/langgraph/`（StateGraph、checkpoint、interrupt；示例 1 已在用户 Mac 跑通，示例 2–4 依赖 Postgres，待用户本机验证）
- [x] FastAPI 学习：`learning/fastapi/`（含 `FASTAPI_GUIDE.md`）
- [x] Docker Compose 学习：`learning/docker-compose/`（README + 两个 demo yml，demo 待用户本机跑）
- [x] 阶段 0 交付物：`.env.template`、`docker-compose.yml`、`docker/api.Dockerfile`、`backend/server/main.py`（`/health`、`/health/ready`）、`backend/package/agentnexus/config/settings.py`。**里程碑已于 2026-10-17 在本机验证通过**（三服务 healthy；`/health` 200；停 postgres 后 `/health/ready` 503，恢复后 ok）。
- [ ] `learning/docker-compose/` 的 demo 待用户自己跑一遍（学习用）。

## 阶段 1 进度（核心骨架）
- [x] 数据层：`storage/postgres`（表）、`repositories/`、`services/runs.py`（submit/claim/complete/fail）、Alembic 迁移 `0001`、compose 的一次性 `migrate` 服务。8 个集成测试通过。
- [x] API：`POST /api/users`、对话 CRUD、`POST /api/conversations/{id}/messages`（202，返回 queued run）、`GET .../messages`、`GET /api/runs/{id}`。用 `X-User-Id` 头做鉴权占位（阶段 5 替换 `server/deps.py` 的 `current_user_id`）。14 个测试通过，compose 内 curl 端到端验证过。
- [x] 派发与执行（单机版）：`runtime/dispatcher.py`（enqueue / 扫描补投 / claim / 超时 / 并发上限 / 启动恢复 / 关停收尾）+ 回声 Agent，decisions/0005，22 个测试通过，容器内端到端验证。**单进程假设：api 只能起 1 个实例、uvicorn 不能开多 workers**
- [x] 执行：LangGraph chatbot（`agents/buildin/chatbot/` 四件套 + `agent.py`）、`models/` 工厂（默认假模型）、AsyncPostgresSaver（`thread_id=run_id`，表由 migrate 服务建）、decisions/0006 已确认（方案 A）。37 个测试通过，容器内两轮对话验证。**真实 LLM 仅 Gemini 验证过**
- [x] SSE 流式推送：`GET /api/runs/{id}/events`（Redis Streams + FastAPI 内置 SSE），decisions/0007 已确认并实现；59 个测试通过；真实 Gemini 端到端推送延迟 4~6ms。学习导读：`docs/guides/message-lifecycle.md`
- [x] 收尾第二批：checkpoint 清理（0010）、统一错误与请求追踪（0011）；第三批：对话管理（0012）。后端 116 个、前端 29 个测试
- [x] 收尾第一批（decisions/0009）：取消（排队中 200 / 执行中 202 / 已结束 409）、重试（复用输入消息）、错误码（原始报错不对外）、刷新后可见、等待过久提示；迁移 0002/0003。后端 83 个、前端 23 个测试
- [x] 前端页面（`web/`，decisions/0008）：fetch 读取 SSE、刷新接上生成中的回复、Nginx 部署（`http://localhost:8080`）。前端 17 个测试、后端 61 个测试；浏览器实测通过

## 下一步
1. 阶段 1 收尾：~~第一批：取消、重试、错误码、刷新后可见~~（已完成，decisions/0009）；第二批：~~checkpoint 清理与存储测量~~（已完成，decisions/0010）、~~统一错误响应与请求日志/request-id~~（已完成，decisions/0011）；~~第三批：对话改标题/归档、消息分页~~（已完成，decisions/0012）。**阶段 1 已全部完成**（仅剩可选的 openai / anthropic 真实 key 验证）。
2. **阶段 2「RAG 知识库」已完成**：0014 选型；0015 文档入口；0016 入库流水线、Qdrant、锁文件；0017 检索 + 重排、知识库页面；0018 预检索 + 检索工具、引用快照、Markdown 按标题切分。学习导读 `docs/guides/rag-pipeline.md`。阶段 2 待办（可选）：改了分块策略后“全部重新处理”的管理命令；交给模型前按相似度去重；多轮追问评测集；删除文档接口。
3. **阶段 3「知识图谱」已完成**（0019）：用户确认引入 Neo4j（派生存储，PostgreSQL 为事实来源）、按知识库手动构建（预估 + 缓存 + 上限）、先只做浏览与溯源。学习导读 `docs/guides/knowledge-graph.md`。待办（可选）：近义实体合并、图谱参与问答（需要多跳评测集）、文档变更后提示重新构建。
4. **阶段 4** 拆为 4.1 / 4.2 / 4.3。**4.1 已完成**（0020）：独立 worker（`python -m agentnexus.worker`，compose 的 worker 服务，可 `--scale worker=2`）+ PostgreSQL 租约队列（SKIP LOCKED、心跳、fencing、事务内 NOTIFY）；run 从 checkpoint 续做；跨进程取消。学习导读 `docs/guides/worker-leases.md`。**4.2 选型草稿 0021 已写**（实验：加固容器 11 个恶意用例全部挡住、新建容器 126ms；持有 socket = 宿主机 root；SSRF 经 host.docker.internal 仍可达，Squid 按 IP 过滤后全部拦下）。**4.2 已完成**（0021）：sandbox 服务（独占 Docker socket，加固一次性容器，工作空间按对话）、MCP 网关（fetch，internal 网络 + Squid 出口代理）、Skills（`backend/skills/*.md` + `load_skill`，工具由技能启用）。compose 新增 sandbox-runtime（只构建镜像）、sandbox、mcp-gateway、egress-proxy 和三个网络；`.env` 需要 `SANDBOX_TOKEN`。学习导读 `docs/guides/tools-sandbox.md`。4.2 待办（可选）：工作区文件下载接口、删除对话时清理工作空间、沙盒镜像加中文字体。**4.3 已完成**（0022）：深度调研对话（`conversations.agent = research`）：计划（结构化输出）→ 审批（run 状态 `awaiting_approval`，放弃租约，`POST /api/runs/{id}/approval`）→ Send 并行子 Agent → 汇总报告（`artifacts` 表 + `GET /api/artifacts/{id}/download`）；迁移 0010。里程碑：并行中 kill -9 worker，只重做未完成的子任务。学习导读 `docs/guides/multi-agent-research.md`。4.3 待办（可选）：按工具审批、孤儿原件清理、审批过期时发事件。**阶段 5 已完成**（0023）：登录（会话 + HttpOnly Cookie、argon2id、CSRF）、部门与三个角色、知识库部门共享（只读）、应用层 + RLS（迁移 0011，api / worker / 迁移三个数据库角色，`storage/roles.py`）、用量明细与看板。学习导读 `docs/guides/auth-tenancy.md`。阶段 5 待办（可选）：登录失败次数限制、换部门后的共享迁移。**阶段 6.1 已完成**（0024）：前端设计系统（`web/src/theme/`：token 同时驱动组件库主题和 CSS 变量，浅色 / 深色）、全局导航栏、对话阅读列、工具调用时间线、引用与文件卡片、各页面统一结构、手机宽度适配；Nginx 对 index.html 不缓存。学习导读 `docs/guides/frontend-design.md`。**6.2 已完成**：从零部署实测（模板 `.env` 原样使用即可启动）、ruff（`backend/ruff.toml`）、CI（`.github/workflows/ci.yml`）、`ARCHITECTURE.md`；沙盒工作空间卷名改为带项目名。**6.3 已完成**：演示数据 `backend/scripts/seed_demo.py`（账号 kefu_zhang / kefu_li / dev_wang，密码为 `.env` 的 DEMO_PASSWORD）、截图 `scripts/screenshots.py` → `docs/images/`、演示脚本 `docs/demo-script.md`、面试资料（两分钟讲述、量化数据速查）。**剩余**：用户按脚本录制演示视频；可选：登录失败次数限制、按工具审批、Python 类型检查、自动生成工单。
3. 模型已切换为 DeepSeek（Anthropic 兼容格式、`LLM_THINKING=disabled`，见 decisions/0013）；用户 `.env` 已配置。省钱约定：自动化测试一律用假模型，真实调用只在验证时做并控制次数。

### 本地运行
- 一键：`docker compose up -d --build`，打开 `http://localhost:8080`。阶段 5 起需要登录：`.env` 里要有 `DB_API_PASSWORD`、`DB_WORKER_PASSWORD`；第一个管理员用 `docker compose exec -e PW='<密码>' api python -m agentnexus.cli create-user --name admin --role admin --password-env PW` 创建。
- 本机已建好的演示账号：admin（管理员）、alice / bob（研发部成员）、mgr（研发部负责人）、carol（市场部成员），密码都是 `.env` 里的 `DEMO_PASSWORD`。阶段 5 之前的用户（如 prod-demo）没有密码，需要 `python -m agentnexus.cli set-password --name <用户名>`。
- 只想看流式效果、不消耗模型额度：`.env` 里设 `LLM_PROVIDER=fake`、`LLM_FAKE_STREAM_DELAY=0.1`。
- 前端开发：见 `web/README.md`（`npm run dev`，端口 5173）。
- 知识图谱：知识库页面 →「知识图谱」标签页 → 构建（确认框里有预估费用）。Neo4j 浏览器控制台 `http://localhost:7474`（账号 neo4j，密码见 .env 的 NEO4J_PASSWORD）。从 PostgreSQL 重建 Neo4j：`docker compose exec api python -m agentnexus.knowledge.graph.sync`。
- 端到端问答评测（花钱，每轮约 $0.008，30 题 + 3 个知识库外的问题）：`python3 backend/eval/eval_answer_api.py`；用量看 `docker compose logs api | grep "llm usage"`。
- 端到端检索评测（不花钱）：`python3 backend/eval/eval_search_api.py`（服务需已启动，会新建一个用户和知识库）。
- 重建向量索引（换模型、Qdrant 数据丢失时）：`docker compose exec api python -m agentnexus.knowledge.reindex`。
- 依赖：改了 `backend/requirements.txt` 要重新生成锁文件（命令写在 requirements.txt 顶部），镜像和测试环境都按 `requirements.lock` 安装。
- 排查问题：响应头 `X-Request-ID` → `docker compose logs api | grep <id>`；`LOG_FORMAT=json` 时每行一个 JSON。
- **iCloud 提醒**：项目在 iCloud 同步的桌面上，会产生 `xxx 2.py` 之类的冲突副本（已在 `.gitignore` 拦截）；建议移到不同步的目录。

### 跑测试
> 代码检查：`cd backend && ruff check .`（CI 里同样会跑）。
> **同一时间只能跑一个 pytest 会话**：测试会删除并重建 `agentnexus_test` 库、清空 Redis 测试库，两个会话并发会互相干扰（阶段 4.1 踩过）。

集成测试用真实 Postgres、Redis 和 Neo4j（先 `docker compose up -d postgres redis neo4j`，并导出 `NEO4J_URL=bolt://localhost:7687`、`NEO4J_PASSWORD`；没有 Neo4j 时图谱测试会跳过；测试只写随机 id 的知识库，结束时删除）；Qdrant 用进程内模式，不需要起服务；两个真实模型测试在本机有模型缓存时才跑（离线加载，没有就跳过）。只导出连接串，不要把整个 `.env`（含 LLM 密钥）导出到测试进程。测试会自己创建/重建 `agentnexus_test` 库，不碰开发库：
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> REDIS_URL=redis://localhost:6379/0 pytest -q
```

## 待办 / 可选
- 路线图可补充：定时/主动触发的 Agent（复用 AgentRun 流水线）。
- ARCHITECTURE.md 增加"agent harness ↔ `agents/` 包"的对应说明。

## 已定决策
见 `docs/decisions/`：0001 技术栈基线；0002 不引入 LlamaIndex，RAG 链路手写；0003 本地环境 Compose + healthcheck；0004 数据层 SQLAlchemy 异步 + Alembic；0005 阶段 1 先用进程内派发器，阶段 4 换 ARQ + lease；0006 对话历史以 messages 表为准、checkpoint 按 run；0007 流式输出：Redis Streams + SSE，数据库为准；0008 前端同源接入与 fetch 读取 SSE；0009 取消、重试与错误码；0010 checkpoint 保留 24 小时后清理；0011 统一错误格式、request-id 与结构化日志；0012 对话管理（归档、按活动排序、游标分页）；0013 模型供应商 DeepSeek（Anthropic 兼容格式、默认关闭思考）；0014 RAG 选型（本地 bge-small-zh、重排、Qdrant 派生索引）；0015 知识库数据模型、原件存储、解析与分块；0016 入库流水线、向量索引与依赖锁文件；0017 检索、重排与检索测试页面；0018 知识库问答（预检索 + 工具、引用快照）；0019 知识图谱（Neo4j 派生存储、手动构建与费用控制）；0020 独立 worker 与 PostgreSQL 租约队列；0021 工具、沙盒与 MCP（独立 sandbox 服务、MCP 网关 + 出口代理、Skills）；0022 深度调研（Send 并行、计划审批、报告产物）；0023 登录、部门隔离（RLS）与用量看板；0024 前端设计系统与界面改版。

## 环境提示
- 用户在 Mac 上开发；之前的云端沙盒没有 Docker，涉及容器的内容需用户本机验证。
- `docs/learning-notes/qa-log.md` 是本地私有笔记（已 gitignore），问答记录到 Q54。
