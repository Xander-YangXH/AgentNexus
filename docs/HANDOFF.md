# 交接说明（当前进度）

> 供新会话（如 Claude Code）快速接上上下文。每完成一个里程碑就更新本文件。

> 完整进度、架构总图与分模块任务清单见 [PROGRESS.md](PROGRESS.md)；本文件只保留最近的交接要点。

## 项目
AgentNexus：企业级多租户知识库与 Agent 平台（RAG、知识图谱、LangGraph 多 Agent、MCP/Skills、沙盒、多租户、仪表盘），用作 2026-2027 大模型算法 / Agent 开发求职项目。远程仓库：https://github.com/Xander-YangXH/AgentNexus

## 当前阶段：ROADMAP 阶段 1 进行中（阶段 0 已完成）
- [x] LangGraph 学习：`learning/langgraph/`（StateGraph、checkpoint、interrupt；示例 1 已在用户 Mac 跑通，示例 2–4 依赖 Postgres，待用户本机验证）
- [x] FastAPI 学习：`learning/fastapi/`（含 `FASTAPI_GUIDE.md`）
- [x] Docker Compose 学习：`learning/docker-compose/`（README + 两个 demo yml，demo 待用户本机跑）
- [x] 阶段 0 交付物：`.env.template`、`docker-compose.yml`、`docker/api.Dockerfile`、`backend/server/main.py`（`/health`、`/health/ready`）、`backend/package/agentnexus/config/settings.py`。**里程碑已于 2026-10-17 在本机验证通过**（三服务 healthy；`/health` 200；停 postgres 后 `/health/ready` 503，恢复后 ok）。
- [ ] `learning/docker-compose/` 的 demo 待用户自己跑一遍（学习用）。

## 阶段 1 进度（核心骨架）
- [x] 数据层：`storage/postgres`（表）、`repositories/`、`services/runs.py`（submit/claim/complete/fail）、Alembic 迁移 `0001`、compose 的一次性 `migrate` 服务。8 个集成测试通过。
- [x] API：`POST /api/users`、对话 CRUD、`POST /api/conversations/{id}/messages`（202，返回 queued run）、`GET .../messages`、`GET /api/runs/{id}`。用 `X-User-Id` 头做鉴权占位（阶段 5 替换 `server/deps.py` 的 `current_user_id`）。14 个测试通过，compose 内 curl 端到端验证过。
- [x] 派发与执行（单机版）：`runtime/dispatcher.py`（enqueue / 扫描补投 / claim / 超时 / 并发上限 / 启动恢复 / 关停收尾）+ 回声 Agent，decisions/0005，22 个测试通过，容器内端到端验证。**单进程假设：api 只能起 1 个实例、uvicorn 不能开多 workers**
- [x] 执行：LangGraph chatbot（`agents/buildin/chatbot/` 四件套 + `agent.py`）、`models/` 工厂（默认假模型）、AsyncPostgresSaver（`thread_id=run_id`，表由 migrate 服务建）、decisions/0006 已确认（方案 A）。37 个测试通过，容器内两轮对话验证。**真实 LLM 调用从未验证**
- [ ] SSE 流式推送；最小 Vue3 页面

## 下一步
1. **用户配置 Gemini 密钥**（`.env`：`LLM_PROVIDER=google`、`LLM_MODEL=<模型名>`、`LLM_API_KEY=<key>`），运行 `docker compose run --rm --no-deps api python backend/scripts/check_llm.py`，把输出（不含密钥）告诉我。重点看：能否调通、content 类型、流式分块数。已新增 `google` provider；真实调用仍未验证。
2. 1.5 SSE 流式输出：事件模型、事件通道、`GET /runs/{id}/events`、断线续传。会改 Agent 契约（`run()` 目前返回完整文本）。
3. 前端页面；取消 run；checkpoint 清理。

### 跑测试
集成测试用真实 Postgres（先 `docker compose up -d postgres`）。测试会自己创建/重建 `agentnexus_test` 库，不碰开发库：
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> pytest -q
```

## 待办 / 可选
- 路线图可补充：定时/主动触发的 Agent（复用 AgentRun 流水线）。
- ARCHITECTURE.md 增加"agent harness ↔ `agents/` 包"的对应说明。

## 已定决策
见 `docs/decisions/`：0001 技术栈基线；0002 不引入 LlamaIndex，RAG 链路手写；0003 本地环境 Compose + healthcheck；0004 数据层 SQLAlchemy 异步 + Alembic；0005 阶段 1 先用进程内派发器，阶段 4 换 ARQ + lease。

## 环境提示
- 用户在 Mac 上开发；之前的云端沙盒没有 Docker，涉及容器的内容需用户本机验证。
- `docs/learning-notes/qa-log.md` 是本地私有笔记（已 gitignore），问答记录到 Q23。
