# 学习记录（Q&A Log）

> 每次讨论后追加一条记录：问了什么、结论是什么、为什么这么定。面试被拷打时先翻这份文档找线索，别去翻聊天记录。

## 2026-09-16

### Q1：Yuxi 项目的技术栈和代码架构是什么？
**结论**：前端 Vue3 + Vite + Ant Design Vue + AntV G6；后端 FastAPI + LangGraph + ARQ worker；存储 PostgreSQL + Redis + MinIO + Milvus + Neo4j；文档处理 MinerU/PaddleX/RapidOCR；部署 Docker Compose。代码分三层：`web`（前端）/ `backend/server`（FastAPI 路由层，刻意保持薄）/ `backend/package/yuxi`（业务逻辑主体，刻意保持厚）。

最有工程含量的是 Agent 运行链路：请求先落库（Postgres）→ 按用户/Agent/线程做 FIFO 排队 → 只有数据库提交成功才投递 ARQ → 独立 worker 用 lease/heartbeat 争抢执行权（防止重复执行）→ 运行事件写 Redis Stream → 前端 SSE 消费 → 最终状态必须落回 Postgres（Redis 不是事实来源）。

**为什么值得记**：这是 AgentNexus 的架构蓝本。很多后续设计决策的本质都是"参考 Yuxi 怎么做，再决定简化哪里、为什么简化"，回答"为什么这么设计"时要能同时说出 Yuxi 原版方案和我们的简化理由。

### Q2：想完整复现这个项目，需要重命名、给学习路线图、建项目文件夹
**结论**：
- 项目改名为 **AgentNexus**（Nexus = 枢纽，呼应"中台"定位：核心能力是枢纽，业务场景包是插在枢纽上的分支）
- 复现范围：全面对标 Yuxi 的**能力广度**（RAG / 知识图谱 / 多智能体 / MCP-Skills / 沙盒 / 多租户 / Dashboard 都要有），但基础设施做轻量化替代——Qdrant 代替 Milvus+etcd，轻量解析库代替 MinerU+PaddleX，worker 内置的 Docker 隔离代替独立 sandbox-provisioner 微服务。完整对照表在 `docs/tech-tradeoffs.md`。
- 时间预算 3-4 个月，路线图拆成 7 个阶段（`ROADMAP.md`），阶段 4（多智能体 + MCP + 沙盒 + 排队 lease 机制）是核心难点，也是面试最值得讲的部分。
- 项目文件夹建在 `~/Desktop/AgentNexus`，已 `git init` 并完成首次提交。

**面试可能会问**："为什么不直接照抄 Yuxi 的完整技术栈？" → 答案在 `docs/tech-tradeoffs.md`：solo 项目要保留每种能力的设计理念，简化其生产级实现规模，这是主动的工程判断，不是能力不够，每条简化都能说清楚"如果要按生产级扩展应该怎么改"。

### Q3：要求以后每次问答都记录学习文档
**结论**：建立本文件（`qa-log.md`）和 `docs/interview-prep.md` 的记录约定，并写进 `AGENTS.md`，确保后续每次讨论都自动更新，不需要每次重新提醒。
