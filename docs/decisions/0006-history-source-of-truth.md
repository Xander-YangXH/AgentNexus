# 0006 - 对话历史的事实来源，与 LangGraph checkpoint 的作用域

**状态**：草稿，待确认（阶段 1.4 开工前必须定）

**背景**：接入 LangGraph 后，“对话里说过什么”会同时出现在两个地方：我们的 `messages` 表，和 LangGraph 的 checkpoint（`PostgresSaver` 把图状态，包括 `messages`，存在 Postgres）。两份数据如果都当“事实”，迟早不一致。需要明确：谁是事实来源，checkpoint 的 `thread_id` 该绑谁。

## 方案

| | 历史以谁为准 | checkpoint 的 thread_id | 每次执行传给图的输入 |
|---|---|---|---|
| A | `messages` 表 | **run_id**（一次执行一个线程） | 从表里取出的全量历史 |
| B | checkpoint | conversation_id | 只传本轮新消息 |
| C | 两边都当真 | conversation_id | 全量历史（表）+ checkpoint 里已有的 |

## 实验结论（langgraph 1.2.15、langgraph-checkpoint-postgres 3.2.0、真实 Postgres）

最小图：一个节点读取 `state["messages"]`，返回 “seen N msgs”，用来看图到底收到了几条消息。

1. **B 可行**：同一 thread 连续调用，历史自动累积（第二轮 seen 3）。
2. **B 下失败的轮次会残留**：第 3 轮节点抛错后，checkpoint 里仍保留了这轮的用户消息；用户重发，图收到的是“原问题 + 重发”两条。
3. **C 会产生重复**：checkpoint 已有历史，又把表里的全量历史再传一遍，得到 `H:a1 A:seen H:a1 A:seen H:a2`，消息翻倍。
4. **C 给消息加稳定 id 只修好一半**：用户消息按 id 去重了（`b1` 只出现一次），但助手消息由图自己生成 id，与库里的不同，仍然重复。要修好就得把库里的 id 回写进 checkpoint，两个存储耦合得更紧。
5. **A 可行**：每次传全量历史、每个 run 独立 thread，历史完全来自表。
6. **A 的 checkpoint 仍有价值，且按 run 恢复可行**：第二个节点抛错后，用同一 thread 调 `invoke(None)` 续跑，从失败节点继续，已完成的第一个节点**重做次数为 0**。这正是阶段 4 “中断审批 / 崩溃后接管”需要的能力。
7. `PostgresSaver` 提供 `delete_thread`，可按 run 清理。

> **更正我之前的一个说法**：我曾说 A 的好处是“失败的 run 不会污染历史”。这不成立——失败 run 的用户消息在提交时就已经落进 `messages` 表，A 和 B 都会让下一轮看到它。A 的真正优势不是“没有残留”，而是**这个策略握在我们手里、看得见、可调整**（见下“失败轮次的处理”），而 B 里它藏在 checkpoint 的二进制状态中。

## 决定（推荐 A，等你确认）

1. **事实来源是 `messages` 表**。图的输入由 `RunContext.history` 转换而来（它本来就是“截止到本次输入消息的历史”），图本身对会话无状态。
2. **checkpoint 的 `thread_id` 用 `run_id`，不用 `conversation_id`**。checkpoint 只保存“一次执行的进行中状态”：工具调用循环、`interrupt` 的暂停点、崩溃后的断点。*这会改动此前“conversations.id 同时作为 thread_id”的说法，确认后需同步 `models.py` 注释、PROGRESS 和 interview-prep。*
3. **阶段 4 的恢复建立在此之上**：中断审批、worker 崩溃后的接管，都是“同一 run_id 续跑”，不需要把历史再喂一遍。
4. **失败轮次的处理**：失败 run 的用户消息保留在历史里（用户在界面上也看得到），下一轮 LLM 会看到“原问题 + 重发”。先接受这个行为；若后续发现影响回答质量，可以在构造 `history` 时过滤掉“对应 run 为 failed 且之后又有新提问”的消息，只需改一个函数。
5. **只有最终的 user / assistant 文本进 `messages` 表**。图内部的工具调用及结果留在该 run 的 checkpoint 里，下一轮不可见。省 token，但丢失工具结果细节；若需要，另建 run_steps 表持久化，不污染 `messages`。
6. **摘要压缩（阶段 4 中间件）作用在“送给图的输入”上**，不修改 `messages` 表，原文始终完整，摘要是派生物。
7. **checkpoint 清理**：每个 run 一个 thread，数据量随 run 数线性增长。run 到终态并过保留期后调用 `delete_thread`；保留期内留着便于排错。这里的增长速度**我没有测过**，上线前要测。

## 后果

- 好处：单一事实来源；排错时看 `messages` 表即可；摘要、过滤、编辑重发这类“改写历史”的需求都只动一处；与阶段 4 的恢复机制自然衔接。
- 代价：每轮都要把全量历史取出并转换；长对话 token 成本与方案 B 相同（无论哪种都要把历史发给 LLM），但读库多一次（有 `(conversation_id, seq)` 索引，不是瓶颈）。
- 风险：消息转换（我们的 `ChatMessage` ↔ LangChain 的 `HumanMessage/AIMessage`）是新的映射点，需要测试覆盖角色映射；工具消息不入库意味着“上一轮的工具结果”对下一轮不可见，需要在产品行为上接受。

## 开放问题（需要你决定）

1. **是否接受方案 A**，尤其是第 2 点（thread_id 改用 run_id）和第 4 点（失败轮次保留）。
2. **LLM 供应商与密钥**。我倾向在 `models/` 做一个很薄的工厂：环境变量 `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL`，先支持两类——OpenAI 兼容接口（`langchain-openai`，改 `base_url` 即可对接 DeepSeek、通义、智谱等）和 Anthropic（`langchain-anthropic`）；测试里用假模型，不依赖密钥；密钥只放本机 `.env`，不进仓库。我**没有**验证过任何真实供应商（没有密钥）。你想用哪一家？

## 验证

实验脚本为一次性的，不进仓库；结论已逐条列在上面。确认方案后，实现时补：消息映射的单元测试、“同一 run 失败后续跑”的集成测试、`thread_id` 为 run_id 的断言。
