# 交接说明（当前进度）

> 供新会话（如 Claude Code）快速接上上下文。每完成一个里程碑就更新本文件。

## 项目
AgentNexus：企业级多租户知识库与 Agent 平台（RAG、知识图谱、LangGraph 多 Agent、MCP/Skills、沙盒、多租户、仪表盘），用作 2026-2027 大模型算法 / Agent 开发求职项目。远程仓库：https://github.com/Xander-YangXH/AgentNexus

## 当前阶段：ROADMAP 阶段 0 已完成，下一步阶段 1
- [x] LangGraph 学习：`learning/langgraph/`（StateGraph、checkpoint、interrupt；示例 1 已在用户 Mac 跑通，示例 2–4 依赖 Postgres，待用户本机验证）
- [x] FastAPI 学习：`learning/fastapi/`（含 `FASTAPI_GUIDE.md`）
- [x] Docker Compose 学习：`learning/docker-compose/`（README + 两个 demo yml，demo 待用户本机跑）
- [x] 阶段 0 交付物：`.env.template`、`docker-compose.yml`、`docker/api.Dockerfile`、`backend/server/main.py`（`/health`、`/health/ready`）、`backend/package/agentnexus/config/settings.py`。**里程碑已于 2026-10-17 在本机验证通过**（三服务 healthy；`/health` 200；停 postgres 后 `/health/ready` 503，恢复后 ok）。
- [ ] `learning/docker-compose/` 的 demo 待用户自己跑一遍（学习用）。

## 下一步
1. 进入阶段 1：核心骨架（用户/线程/消息落库、AgentRun 先落库再投递、SSE、LangGraph + PostgresSaver）。

## 待办 / 可选
- 把本地未推送的提交推到 GitHub。
- 路线图可补充：定时/主动触发的 Agent（复用 AgentRun 流水线）。
- ARCHITECTURE.md 增加"agent harness ↔ `agents/` 包"的对应说明。

## 已定决策
见 `docs/decisions/`：0001 技术栈基线；0002 不引入 LlamaIndex，RAG 链路手写；0003 本地环境 Compose + healthcheck。

## 环境提示
- 用户在 Mac 上开发；之前的云端沙盒没有 Docker，涉及容器的内容需用户本机验证。
- `docs/learning-notes/qa-log.md` 是本地私有笔记（已 gitignore），问答记录到 Q12。
