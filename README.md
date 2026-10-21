# AgentNexus

## 项目定位

AgentNexus 是一个「中台」型 Agent 平台：核心能力（Agent 编排、知识库、知识图谱、工具 / MCP 扩展、沙盒执行、权限）可复用，具体业务以「场景包」形式插拔接入。当前完整落地的场景包是 **智能客服与工单自动化**，其余场景包先留接口占位（stub）。

设计目标覆盖 RAG 知识库、知识图谱、LangGraph 多智能体编排、可扩展工具协议、沙盒工具执行、多租户权限、运行监控看板这几类能力；每一项技术选型的推导过程见 [docs/interview-prep.md](docs/interview-prep.md)，基础设施规模按 solo 项目的体量做了取舍，具体见 [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md)。

## 技术栈（目标状态）

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Ant Design Vue |
| 后端 API | FastAPI |
| Agent 编排 | LangGraph + LangChain |
| 向量检索 | Qdrant |
| 关系型存储 | PostgreSQL |
| 缓存 / 队列 | Redis + ARQ |
| 对象存储 | MinIO |
| 知识图谱 | Neo4j |
| 文档解析 | pypdf / python-docx / unstructured |
| 工具沙盒 | Docker SDK 容器隔离（内置于 worker） |
| 部署 | Docker Compose |

## 快速开始

```bash
cp .env.template .env              # 修改 POSTGRES_PASSWORD；默认 LLM_PROVIDER=fake，无需任何密钥
docker compose up -d --build
```

打开 http://localhost:8080 ：创建一个用户 → 新对话 → 发消息，回复会逐字出现。
接入真实模型：在 `.env` 里设置 `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`（说明见 `.env.template`），然后 `docker compose up -d api`。

## 文档索引

- [ROADMAP.md](ROADMAP.md) — 分阶段学习与开发路线图（3-4 个月）
- [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md) — 基础设施与能力深度的取舍及理由
- `docs/decisions/` — 关键架构决策记录（每次重大取舍写一条，含背景/决定/后果/验证）
- [docs/guides/message-lifecycle.md](docs/guides/message-lifecycle.md) — 学习导读：一条消息从发送到流式显示的完整旅程（代码位置、设计理由、故障表现、自测题）
- [docs/PROGRESS.md](docs/PROGRESS.md) — 进度与架构总图：当前位置、主链路、数据模型、分模块任务清单（勾选）
- [docs/dev-log.md](docs/dev-log.md) — 开发日志：每一步的设计思路、取舍与踩坑记录
- `docs/interview-prep.md` — 面试拷打准备文档，解释每个模块的设计原因和业务逻辑
- `docs/learning-notes/` — 按主题记录的学习笔记（含本地私有的问答记录，未纳入版本管理）
- `ARCHITECTURE.md` — 随开发推进逐步补全（边界、目录职责、核心链路、不变量）

## 目录结构

```
AgentNexus/
├── backend/
│   ├── server/                  # FastAPI 入口 + 路由层（薄）
│   └── package/agentnexus/      # 业务逻辑包（厚）
│       ├── agents/              # LangGraph 智能体、middleware、toolkits
│       ├── knowledge/           # 文档解析、分块、向量检索、图谱抽取
│       ├── workspace/           # 用户工作区 / 沙盒文件路径管理
│       ├── services/            # 用例层
│       ├── repositories/        # 持久化访问层
│       ├── storage/             # postgres/redis/minio/neo4j 连接管理
│       ├── models/              # 模型供应商适配
│       └── config/              # 系统 / 用户配置
├── web/                         # Vue3 前端
├── docker/                      # 各服务 Dockerfile
├── docs/
├── scripts/
└── docker-compose.yml           # 阶段 0：postgres + redis + api，后续逐步叠加
```

## 当前进度

- [x] 阶段 0：环境与技术选型
- [ ] 阶段 1：核心骨架与 Agent 编排引擎（里程碑已达成：网页流式聊天、刷新不丢；收尾项见 PROGRESS）
- [ ] 阶段 2：RAG 知识库
- [ ] 阶段 3：知识图谱
- [ ] 阶段 4：多智能体、工具与扩展生态
- [ ] 阶段 5：多租户权限与 Dashboard
- [ ] 阶段 6：打磨、部署与简历包装

详见 [ROADMAP.md](ROADMAP.md)。
