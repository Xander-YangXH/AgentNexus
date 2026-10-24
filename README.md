# AgentNexus

## 项目定位

AgentNexus 是一个「中台」型 Agent 平台：核心能力（Agent 编排、知识库、知识图谱、工具 / MCP 扩展、沙盒执行、权限）可复用，具体业务以「场景包」形式插拔接入。当前完整落地的场景包是 **智能客服与工单自动化**，其余场景包先留接口占位（stub）。

设计目标覆盖 RAG 知识库、知识图谱、LangGraph 多智能体编排、可扩展工具协议、沙盒工具执行、多租户权限、运行监控看板这几类能力；每一项技术选型的推导过程见 [docs/interview-prep.md](docs/interview-prep.md)，基础设施规模按 solo 项目的体量做了取舍，具体见 [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md)。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + Ant Design Vue |
| 后端 API | FastAPI |
| Agent 编排 | LangGraph（checkpoint 续做、interrupt 审批、Send 并行子 Agent）+ LangChain 模型适配 |
| 向量检索 | Qdrant |
| 关系型存储 | PostgreSQL（事实来源；行级安全做第二道隔离）|
| 任务队列 | PostgreSQL 租约队列（SKIP LOCKED + 心跳 + fencing token，实验后替代 ARQ）|
| 实时事件 | Redis Streams + SSE（断线续传）|
| 登录 | 服务端会话 + HttpOnly Cookie，argon2id |
| 对象存储 | 本地数据卷（`BlobStore` 接口，可换 MinIO / S3） |
| 知识图谱 | Neo4j（派生存储，PostgreSQL 为事实来源）+ cytoscape.js |
| 文档解析 | pypdf / python-docx |
| Embedding / 重排 | bge-small-zh-v1.5、bge-reranker-v2-m3（fastembed / ONNX，本地运行） |
| 模型 | DeepSeek（Anthropic 兼容接口）；默认假模型，无需密钥即可跑通 |
| 工具沙盒 | 独立 sandbox 服务（唯一持有 Docker socket）+ 加固的一次性容器；MCP 网关 + Squid 出口代理 |
| 部署 | Docker Compose（12 个服务，一键启动）；GitHub Actions CI |

## 快速开始

```bash
cp .env.template .env              # 修改各项密码（POSTGRES / NEO4J / SANDBOX_TOKEN / DB_API / DB_WORKER）；默认 LLM_PROVIDER=fake，无需模型密钥
docker compose up -d --build
docker compose exec -e PW='<管理员密码>' api python -m agentnexus.cli create-user --name admin --role admin --password-env PW
```

打开 http://localhost:8080 ：用 admin 登录 → 在“管理”里建部门和用户 → 新对话 → 发消息，回复会逐字出现。
接入真实模型：在 `.env` 里设置 `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`（说明见 `.env.template`），然后 `docker compose up -d api`。

## 文档索引

- [ARCHITECTURE.md](ARCHITECTURE.md) — 架构：进程拓扑、代码地图、核心链路、架构不变量
- [ROADMAP.md](ROADMAP.md) — 分阶段学习与开发路线图（3-4 个月）
- [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md) — 基础设施与能力深度的取舍及理由
- `docs/decisions/` — 关键架构决策记录（每次重大取舍写一条，含背景/决定/后果/验证）
- [docs/guides/message-lifecycle.md](docs/guides/message-lifecycle.md) — 学习导读：一条消息从发送到流式显示的完整旅程（代码位置、设计理由、故障表现、自测题）
- [docs/guides/worker-leases.md](docs/guides/worker-leases.md) — 学习导读：独立 worker 与租约（认领、心跳、fencing、续做、跨进程取消、崩溃接管实测）
- [docs/guides/knowledge-graph.md](docs/guides/knowledge-graph.md) — 学习导读：知识图谱的抽取、合并、存储分工、失败与恢复、自测题
- [docs/guides/frontend-design.md](docs/guides/frontend-design.md) — 学习导读：前端设计系统（token 如何驱动组件库、CSS 变量与图表，Agent 工作的可视化，改版踩坑）
- [docs/guides/auth-tenancy.md](docs/guides/auth-tenancy.md) — 学习导读：登录、部门隔离与用量（会话 vs JWT、CSRF、RLS 的价值与陷阱、用量采集、自测题）
- [docs/guides/multi-agent-research.md](docs/guides/multi-agent-research.md) — 学习导读：深度调研（Send 并行与崩溃续做、interrupt 审批的陷阱、等待审批放弃租约、报告产物、自测题）
- [docs/guides/tools-sandbox.md](docs/guides/tools-sandbox.md) — 学习导读：工具、沙盒与 MCP（威胁模型、socket 风险、SSRF 与出口代理、Skills、自测题）
- [docs/guides/rag-pipeline.md](docs/guides/rag-pipeline.md) — 学习导读：一份文档从上传到成为回答里的引用（入库、检索、重排、预检索与工具、三层评测、自测题）
- [docs/PROGRESS.md](docs/PROGRESS.md) — 进度与架构总图：当前位置、主链路、数据模型、分模块任务清单（勾选）
- [docs/dev-log.md](docs/dev-log.md) — 开发日志：每一步的设计思路、取舍与踩坑记录
- `docs/interview-prep.md` — 面试拷打准备文档，解释每个模块的设计原因和业务逻辑
- `docs/learning-notes/` — 按主题记录的学习笔记（含本地私有的问答记录，未纳入版本管理）

## 目录结构

完整的代码地图、进程拓扑、核心链路和架构不变量见 [ARCHITECTURE.md](ARCHITECTURE.md)。

```
AgentNexus/
├── backend/
│   ├── server/                  # FastAPI 入口 + 路由层（薄）
│   ├── package/agentnexus/      # 业务逻辑（厚）：agents、runtime、services、knowledge、security、sandbox、mcp_gateway……
│   ├── migrations/              # Alembic 迁移（只往前加）
│   ├── skills/                  # Agent 技能文件
│   ├── tests/                   # 集成测试（真实 PostgreSQL / Redis / Neo4j / Docker）
│   └── eval/                    # 选型实验与评测脚本
├── web/                         # Vue 3 + Vite + Ant Design Vue 前端
├── docker/                      # 各镜像 Dockerfile、Nginx、Squid 配置
├── docs/                        # 决策记录、走读、开发日志、面试准备
├── .github/workflows/ci.yml     # CI：lint + 测试 + 构建
└── docker-compose.yml           # 一键启动全部服务
```

## 当前进度

- [x] 阶段 0：环境与技术选型
- [x] 阶段 1：核心骨架与 Agent 编排引擎（流式聊天、取消 / 重试、请求追踪、对话管理；走读见 docs/guides/message-lifecycle.md）
- [x] 阶段 2：RAG 知识库（上传 → 后台入库 → 本地向量化 + Qdrant → 召回 + 重排 → 预检索与检索工具 → 带引用的回答；三层评测；走读见 docs/guides/rag-pipeline.md）
- [x] 阶段 3：知识图谱（LLM 结构化抽取 + 缓存与费用预估 → 合并 → PostgreSQL 事实来源 + Neo4j 派生存储 → 图谱浏览与溯源；走读见 docs/guides/knowledge-graph.md）
- [x] 阶段 4：多智能体、工具与扩展生态（独立 worker + PostgreSQL 租约队列，走读见 docs/guides/worker-leases.md；沙盒执行代码、MCP 网关 + 出口代理、Skills，走读见 docs/guides/tools-sandbox.md；深度调研：计划审批 + 并行子 Agent + 可下载报告，崩溃后只重做未完成的子任务，走读见 docs/guides/multi-agent-research.md）
- [x] 阶段 5：多租户权限与 Dashboard（会话登录、部门共享、应用层 + PostgreSQL RLS 双重隔离、用量看板，走读见 docs/guides/auth-tenancy.md）
- [ ] 阶段 6：打磨、部署与简历包装（6.1 已完成：前端设计系统与改版，浅色 / 深色主题，走读见 docs/guides/frontend-design.md）

详见 [ROADMAP.md](ROADMAP.md)。
