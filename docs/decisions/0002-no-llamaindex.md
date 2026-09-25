# 0002 - RAG 管线不引入 LlamaIndex

**状态**：已决定（阶段 0）

**背景**：LlamaIndex 是专精数据接入/索引/检索的框架，跟项目已经在用的 LangChain/LangGraph 编排层不是互斥关系，业内也有两者组合使用的模式（LlamaIndex 管检索、LangGraph 管决策流程）。需要判断是否在阶段 2 的 RAG pipeline 里引入它。

**决定**：不引入。RAG pipeline（解析 → 分块 → 向量化 → 检索）自己用 LangChain 的 text splitter 辅助手写，直接对接 Qdrant，不使用 LlamaIndex 的 VectorStoreIndex/QueryEngine 等高层封装。

**理由**：阶段 2 的学习目标是吃透 RAG 每一步机制，用高层封装会让分块策略、召回调优这些细节变成黑盒，不利于面试时讲清楚具体实现；同时避免引入第二套框架生态增加的依赖面和学习成本。

**后果**：RAG 相关代码需要自己维护解析/分块/检索逻辑，工作量比直接用 LlamaIndex 现成组件大，但换来的是每个环节都可解释、可调试、可在面试中讲清楚设计依据。

**验证**：阶段 2 完成后用真实评估集跑出检索召回率/相关性指标，证明自己实现的管线效果达标。
