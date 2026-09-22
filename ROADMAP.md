# ROADMAP

按 3-4 个月（约 14-16 周）规划，每周投入按业余/半脱产强度估算，可根据实际进度压缩或拉长阶段边界。每个阶段给出：学习目标、要产出的代码/文档、验收里程碑、参考资料。

## 阶段 0 · 环境与技术选型（第 1 周）

**学习目标**
- 过一遍 LangGraph 官方文档的 StateGraph / checkpoint / interrupt 机制
- FastAPI 异步基础（依赖注入、后台任务、SSE）
- Docker Compose 多服务编排

**产出**
- 确定 docs/tech-tradeoffs.md 里的技术选型（可以直接用本文档给的默认值，也可以替换）
- 初始化 monorepo 骨架（已完成）、`.env.template`、最初版 `docker-compose.yml`（先只起 postgres + redis）

**里程碑**：`docker compose up` 能起来一个空的 FastAPI + Postgres + Redis，`/health` 返回 200。

**参考资料**：https://langchain-ai.github.io/langgraph/ ；https://fastapi.tiangolo.com/

---

## 阶段 1 · 核心骨架与 Agent 编排引擎（第 2-4 周）

**学习目标**
- LangGraph 的 StateGraph 定义、checkpoint（用 Postgres 做持久化）
- 一个最小可行的「请求落库 → 派发执行 → SSE 推流」链路怎么设计

**产出**
- `backend/package/agentnexus/agents/base.py`：BaseAgent / BaseContext
- `backend/package/agentnexus/agents/buildin/chatbot/`：graph.py + state.py + prompt.py（按 graph/state/prompt/context 四件套拆分，职责单一、方便单测）
- 请求先写 Postgres（conversation + message），再交给一个简单队列（可以先用 asyncio.Queue 单机版，阶段 4 前再升级成 ARQ 真正的独立 worker）
- 前端 `web/`：最小 Vue3 页面，能发消息、流式收到回复

**里程碑**：网页里能和一个基础 LangGraph agent 聊天，回复是流式的，历史记录能刷新页面后还在。

**这一阶段建议写第一篇 docs/decisions/ 记录**：为什么先用单机队列而不是一开始就上 ARQ。

---

## 阶段 2 · RAG 知识库（第 5-7 周）

**学习目标**
- 文档解析与分块策略（可以参考 RAGFlow 的分块思路，不用整套照搬）
- 向量库选型与使用（Qdrant 比 Milvus 部署更轻，适合 solo 项目）
- Embedding / Rerank 模型接入

**产出**
- `knowledge/parser`：先支持 PDF/Word/Markdown 的轻量解析（pypdf、python-docx、unstructured 三选一起步）
- `knowledge/chunking`：按段落/语义边界切分 + token 统计
- `knowledge/implementations/qdrant.py`：向量入库与检索
- 检索测试页面（输入 query，看召回片段和分数）
- Agent 接入一个 `knowledge_search` 工具，回答时引用检索片段

**里程碑**：上传几份文档，Agent 能基于检索内容正确回答，且能看到它引用了哪些片段。

**参考资料**：RAGFlow 分块源码 https://github.com/infiniflow/ragflow ；Qdrant 文档 https://qdrant.tech/documentation/

---

## 阶段 3 · 知识图谱（第 8-9 周）

**学习目标**
- 用 LLM 做实体/关系抽取（prompt 让模型输出结构化的实体/关系 JSON，再写入图数据库）
- Neo4j 基础：Cypher 查询、Python driver
- 图可视化组件（AntV G6 或更轻量的方案）

**产出**
- `knowledge/graphs/`：从已入库的知识块中抽取实体关系，写入 Neo4j
- 前端一个简单的图谱浏览页面：关键词搜实体、点节点看属性、高亮关联子图

**里程碑**：选一个知识库，能看到自动抽取出的实体关系图，点击节点展开关联。

**参考资料**：Neo4j GraphAcademy https://graphacademy.neo4j.com/ ；LightRAG（早期图谱构建思路）https://github.com/HKUDS/LightRAG

---

## 阶段 4 · 多智能体、工具与扩展生态（第 10-12 周）

这是整个项目里工程含量最高、也最值得在简历/面试里讲的部分。

**学习目标**
- LangGraph 多 Agent 编排模式（supervisor 派发 + subagent 并行执行）
- MCP 协议基础，用 `langchain-mcp-adapters` 接入一个现成的 MCP server
- Skills 机制（可以简化成一种「按需加载的工具配置」，不需要支持在线插件市场）
- 工具沙盒执行（用 Docker SDK 起隔离容器跑文件读写/代码执行类工具，比独立的 provisioner 微服务简化很多，但要保留「沙盒隔离」这个核心概念）
- 这一阶段把「请求排队」从阶段 1 的 asyncio.Queue 升级成真正的 ARQ + 独立 worker 容器，加上简单的 lease/heartbeat 防止任务被重复执行——这是分布式任务执行里最值得练的设计

**产出**
- `agents/middlewares/`：subagent 派发、摘要压缩、审批中断
- `agents/backends/sandbox/`：Docker 容器隔离的工具执行后端
- 一个「深度调研」类多步任务 Agent：能拆解任务、派子 Agent 并行执行、汇总产出可下载文件
- 接入至少一个 MCP server
- `worker` 独立容器 + ARQ + lease 机制

**里程碑**：给 Agent 一个复杂任务（比如"调研 A/B/C 三个主题分别写一段"），能看到它拆解成子任务、并行执行、最后汇总成一份可下载的报告；杀掉 worker 进程再重启，未完成的任务能被正确接管而不是卡死或重复执行。

**参考资料**：DeepAgents（深度智能体框架）https://github.com/langchain-ai/deepagents ；DeerFlow（沙盒智能体架构）https://github.com/bytedance/deer-flow ；MCP 规范 https://modelcontextprotocol.io/

---

## 阶段 5 · 多租户权限与 Dashboard（第 13-14 周）

**学习目标**
- 基础 RBAC 模型设计
- FastAPI 依赖注入做权限校验

**产出**
- 简化版用户/部门/权限体系：不需要企业级系统那么细的权限矩阵，但要有"用户只能看到自己和本部门的 Agent / 知识库"这种基本隔离
- 运行监控 Dashboard：请求量、Token 消耗、知识库检索频次的简单统计图表

**里程碑**：用两个不同角色的账号登录，能看到彼此看不到对方数据的实际效果；Dashboard 上能看到真实的用量曲线。

---

## 阶段 6 · 打磨、部署与简历包装（第 15-16 周）

**产出**
- 完整 `docker-compose.yml` 一键起全部服务
- 补全 `ARCHITECTURE.md`（参考成熟项目常见的架构文档写法：鸟瞰、代码地图、核心运行链路、架构不变量）
- 录制一段 3-5 分钟的演示视频（客服工单场景端到端 + Agent 并行任务场景）
- 准备好面试要讲的技术故事，重点是阶段 4 的排队/lease/恢复机制和阶段 2-3 的 RAG+知识图谱设计取舍

**里程碑**：项目可以从零 `git clone` + `docker compose up` 跑起来，README 里的截图和视频链接齐全。

---

## 节奏提醒

阶段 4 是全项目最重、最容易拖期的部分，如果时间紧张，可以砍的优先级从低到高：Skills 在线安装能力 > Dashboard 图表丰富度 > 多租户权限细粒度 > 知识图谱可视化交互 > 沙盒隔离（这个不建议砍，是最有工程亮点的部分）。
