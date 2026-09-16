# AgentNexus

> 参考 [xerrors/Yuxi](https://github.com/xerrors/Yuxi)（可私有部署的多租户知识智能体平台）的架构重构而来的简历项目，面向 2026-2027 秋招 / 暑期实习「大模型算法 / Agent 开发」方向。

## 项目定位

AgentNexus 是一个「中台」型 Agent 平台：核心能力（Agent 编排、知识库、知识图谱、工具 / MCP / Skills 扩展、沙盒执行、权限）可复用，具体业务以「场景包」形式插拔接入。当前完整落地的场景包是 **智能客服与工单自动化**，其余场景包先留接口占位（stub）。

目标是在技术广度上对标 Yuxi：RAG 知识库、知识图谱、LangGraph 多智能体编排、MCP/Skills、沙盒工具、多租户权限、运行监控 Dashboard 都要有实现；基础设施按 solo 项目的体量做了轻量化取舍，具体见 [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md)。

## 技术栈（目标状态）

| 层 | AgentNexus 选型 | 对应 Yuxi 原版 |
|---|---|---|
| 前端 | Vue 3 + Vite + Ant Design Vue | 相同 |
| 后端 API | FastAPI | 相同 |
| Agent 编排 | LangGraph + LangChain | 相同 |
| 向量检索 | Qdrant | Milvus + etcd |
| 关系型存储 | PostgreSQL | 相同 |
| 缓存 / 队列 | Redis + ARQ | 相同 |
| 对象存储 | MinIO | 相同 |
| 知识图谱 | Neo4j | 相同 |
| 文档解析 | pypdf / python-docx / unstructured | MinerU + PaddleX + RapidOCR |
| 工具沙盒 | Docker SDK 容器隔离（内置于 worker） | 独立 agent-sandbox provisioner 微服务 |
| 部署 | Docker Compose | 相同 |

## 文档索引

- [ROADMAP.md](ROADMAP.md) — 分阶段学习与开发路线图（3-4 个月）
- [docs/tech-tradeoffs.md](docs/tech-tradeoffs.md) — 与 Yuxi 的技术取舍对照及理由
- `docs/decisions/` — 关键架构决策记录（仿 Yuxi 的 decision log 习惯，每次重大取舍写一条）
- `docs/learning-notes/` — 按主题记录的学习笔记
- `ARCHITECTURE.md` — 随开发推进逐步补全（仿照 Yuxi 的写法：边界、目录职责、核心链路、不变量）

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
└── docker-compose.yml           # 阶段 1 起逐步补全
```

## 当前进度

- [ ] 阶段 0：环境与技术选型
- [ ] 阶段 1：核心骨架与 Agent 编排引擎
- [ ] 阶段 2：RAG 知识库
- [ ] 阶段 3：知识图谱
- [ ] 阶段 4：多智能体、工具与扩展生态
- [ ] 阶段 5：多租户权限与 Dashboard
- [ ] 阶段 6：打磨、部署与简历包装

详见 [ROADMAP.md](ROADMAP.md)。
