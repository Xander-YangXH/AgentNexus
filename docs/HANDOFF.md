# 交接说明（当前进度）

> 供新会话（如 Claude Code）快速接上上下文。每完成一个里程碑就更新本文件。

> 完整进度、架构总图与分模块任务清单见 [PROGRESS.md](PROGRESS.md)；本文件只保留最近的交接要点。

## 项目
AgentNexus：企业级多租户知识库与 Agent 平台（RAG、知识图谱、LangGraph 多 Agent、MCP/Skills、沙盒、多租户、仪表盘），用作 2026-2027 大模型算法 / Agent 开发求职项目。远程仓库：https://github.com/Xander-YangXH/AgentNexus

## 当前阶段：ROADMAP 阶段 1 里程碑已达成，正在收尾（阶段 0 已完成）
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
- [x] 前端页面（`web/`，decisions/0008）：fetch 读取 SSE、刷新接上生成中的回复、Nginx 部署（`http://localhost:8080`）。前端 17 个测试、后端 61 个测试；浏览器实测通过

## 下一步
1. 阶段 1 收尾：~~第一批：取消、重试、错误码、刷新后可见~~（已完成，decisions/0009）；第二批：checkpoint 清理与存储测量、统一错误响应与请求日志/request-id；第三批：对话改标题/归档、消息分页。
2. 进入阶段 2「RAG 知识库」：先写向量库与 embedding 选型的 decision 草稿（含实验），再动手。
3. 待用户决定：何时推送；是否把 README 的阶段 1 勾上（里程碑已达成，收尾项未完成）。

### 本地运行
- 一键：`docker compose up -d --build`，打开 `http://localhost:8080`。
- 只想看流式效果、不消耗模型额度：`.env` 里设 `LLM_PROVIDER=fake`、`LLM_FAKE_STREAM_DELAY=0.1`。
- 前端开发：见 `web/README.md`（`npm run dev`，端口 5173）。

### 跑测试
集成测试用真实 Postgres 和 Redis（先 `docker compose up -d postgres redis`）。只导出连接串，不要把整个 `.env`（含 LLM 密钥）导出到测试进程。测试会自己创建/重建 `agentnexus_test` 库，不碰开发库：
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> REDIS_URL=redis://localhost:6379/0 pytest -q
```

## 待办 / 可选
- 路线图可补充：定时/主动触发的 Agent（复用 AgentRun 流水线）。
- ARCHITECTURE.md 增加"agent harness ↔ `agents/` 包"的对应说明。

## 已定决策
见 `docs/decisions/`：0001 技术栈基线；0002 不引入 LlamaIndex，RAG 链路手写；0003 本地环境 Compose + healthcheck；0004 数据层 SQLAlchemy 异步 + Alembic；0005 阶段 1 先用进程内派发器，阶段 4 换 ARQ + lease；0006 对话历史以 messages 表为准、checkpoint 按 run；0007 流式输出：Redis Streams + SSE，数据库为准；0008 前端同源接入与 fetch 读取 SSE；0009 取消、重试与错误码。

## 环境提示
- 用户在 Mac 上开发；之前的云端沙盒没有 Docker，涉及容器的内容需用户本机验证。
- `docs/learning-notes/qa-log.md` 是本地私有笔记（已 gitignore），问答记录到 Q29。
