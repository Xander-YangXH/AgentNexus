# 深度调研：计划审批、并行子 Agent 与崩溃续做

> 配套《独立 worker 与租约》《工具、沙盒与 MCP》。阶段 4.3 实现了路线图的阶段 4 里程碑：
> 给 Agent 一个复杂任务，它先给出计划请你确认，然后拆成子任务**并行**执行，最后汇总成一份**可下载**的报告；
> 执行中杀掉 worker，任务被接管，**已完成的子任务不重做**。
> 决策记录：0022（含两个零费用实验）。代码：`agents/buildin/research/`、`runtime/dispatcher.py`、`services/run_events.py`、`services/artifacts.py`。

## 0. 总图

```
用户：新建“深度调研”对话，发任务
  │
worker 认领 run（attempt 1）
  ├─ plan：调用模型，结构化输出 2~5 个子任务
  ├─ approve：interrupt({计划, 预计调用次数})          ← 只有 interrupt，没有模型调用
  └─ 图停在 approve → Agent 返回 ApprovalRequired
派发器：run → awaiting_approval，清空租约（不占执行槽），发 run.awaiting_approval；SSE 在这里结束响应
  │
用户：批准 / 写修改意见 / 取消
  POST /api/runs/{id}/approval → run → queued（attempts 清零）+ NOTIFY
  │
任意 worker 认领（attempt 1）→ Agent：图里有待答复的中断 → Command(resume=答复)
  ├─ 不批准：回到 plan（带上一版计划和意见）→ 再次等待审批
  └─ 批准：Send 扇出 ─┬─ 子 Agent 0（独立上下文，检索 / fetch，最多 4 轮）
                       ├─ 子 Agent 1           同时最多 3 个（max_concurrency）
                       └─ 子 Agent 2
                     → write：汇总成 Markdown 报告（只有这一步的 token 流给用户）→ 保存产物
派发器：助手消息 + 产物元数据同一事务（带租约版本）→ run.completed
```

## 1. 两个实验，两条规则
**规则一：每个子任务必须是独立的任务（Send）。**
- LangGraph 在同一步里并行执行多个任务时，每完成一个就把它的输出作为 pending write 写进 checkpoint。
- 实验：4 个子任务，2 个完成时 `kill -9`，续做只重做另外 2 个；同样 4 个子任务写在一个节点里循环，4 个全部重做。
- compose 实测：3 个子任务，第 1 个完成后 `kill -9` worker，接管日志是 `resuming research from checkpoint before ['research', 'research']`——只剩 2 个。

**规则二：有成本或副作用的步骤，不能和 interrupt 放在同一个节点。**
- 恢复时，含 `interrupt()` 的节点**从头执行**，`interrupt()` 之前的代码再跑一遍。
- 实验：模型调用和 `interrupt()` 写在一起，恢复时模型又被调用一次；模拟“真实模型每次输出不同”后，**用户批准的计划和实际执行的计划不一样**。
- 所以 `plan`（调用模型）和 `approve`（只有 interrupt）是两个节点。compose 实测：给了一次修改意见、再批准，计划只生成了 2 次（每版一次）。

## 2. 等待审批：为什么复用“崩溃接管”的机制
- 等待多久取决于人，不能让 worker 挂着等。于是“等待审批”= 结束这次执行、放弃租约；“答复”= 把 run 改回 queued。
- 之后任何 worker 认领它，都和“接管一个被打断的 run”走同一条路：读 checkpoint，从断点继续。
- Agent 只需要判断“从哪里开始”（`ResearchAgent._starting_point`）：

| checkpoint 里的情况 | 做什么 |
|---|---|
| 有待答复的中断，且有答复 | `Command(resume=答复)`：审批后继续 |
| 有待答复的中断，没有答复 | 继续等（返回 ApprovalRequired）：答复前被接管 |
| 图没跑完 | 从断点继续：崩溃接管 |
| 图跑完了 | 直接用最终状态：完成后没来得及落库 |
| 没有 checkpoint | 新任务 |

- 几个细节：
  - **仍算活跃 run**：同一对话等待期间不能发新消息（避免两个 run 写同一个 checkpoint）；
  - **attempts 清零**：审批是人的动作，不算“被打断”，否则多改几次计划就会耗尽接管次数上限；
  - **带租约版本**：被接管的旧执行者不能把 run 改成等待审批；
  - **过期**：等待超过 24 小时自动取消（`approval_expired`），checkpoint 之后照常回收。

## 3. 事件流在审批处“暂停”
- 服务端发完 `run.awaiting_approval` 就结束 SSE 响应，前端记下这个事件的 id；答复后带 `Last-Event-ID` 重新订阅，后面的进度接着来。
- 刷新页面会从头回放：如果回放到一条**已经答复过**的旧审批事件就结束，后面的进度就看不到了。所以遇到暂停事件先查数据库，run 已不在等待就跳过它。
- 等待时没有订阅，`run.cancelled` 也就收不到：前端点“取消”后自己更新状态、从数据库刷新（浏览器里才发现的问题）。

## 4. 子 Agent：隔离与边界
- **上下文隔离**：每个子 Agent 只看到总任务和自己的子任务；汇总只拿各自的结论和出处，不拿过程。这也是控制上下文长度的方法。
- **工具边界**：只有对话关联知识库的检索和 MCP 的 fetch。网页内容可能有提示注入，所以子 Agent 没有执行代码、写文件的工具；汇总节点没有任何工具。
- **失败隔离**：一个子任务失败（模型报错、工具不可用）记为“失败”，报告里如实说明，不拖垮整份报告。取消是 CancelledError，不会被这里吞掉。
- **进度**：节点里拿不到 run 的事件出口，用 LangGraph 的自定义流（`get_stream_writer`）发 `research.subtask` 和工具事件，由 Agent 转发。

## 5. 报告产物
- 汇总节点把报告写进原件存储（键 `artifacts/<id>`），返回描述；派发器在“完成 run”的同一事务里写 `artifacts` 元数据和消息的附件快照，带租约版本——被接管的旧执行者写不进来。
- 下载 `GET /api/artifacts/{id}/download`：按用户校验（别人的与不存在同样 404）；`Content-Disposition: attachment` + `filename*`（中文文件名）+ `nosniff`，模型写的内容不会被浏览器当成 HTML 渲染。
- 前端要带 `X-User-Id` 头，普通链接带不上，所以先 fetch 成 Blob 再触发下载。

## 6. 实测（compose，真实 DeepSeek）
| 场景 | 结果 |
|---|---|
| 计划 → 修改意见 → 新计划 → 批准 → 2 个子 Agent 并行 fetch → 报告 | 批准后 9 秒完成；7 次模型调用（计划 2、子 Agent 4、汇总 1），约 $0.004 |
| 3 个子任务并行中 kill -9 worker | 约 30 秒后接管，只重做 2 个未完成的子任务；1 条回复、1 份报告；约 $0.004 |
| 下载 | 本人 200，别的用户 404；内容与消息正文一致 |

## 7. 自测题
1. 子任务为什么要用 Send 扇出，而不是在一个节点里 `asyncio.gather`？崩溃后两者分别重做什么？
2. 恢复时含 `interrupt()` 的节点会怎样执行？为什么“模型调用和 interrupt 在同一节点”会让审批失效？
3. 等待审批的 run 为什么要放弃租约？为什么它仍然算“活跃”？
4. 答复时为什么把 attempts 清零？不清零会出现什么问题？
5. `ResearchAgent` 怎么用一个判断同时处理“审批后继续”“崩溃接管”“答复前被接管”？
6. SSE 为什么在审批处结束响应？刷新页面时旧的审批事件为什么要跳过？
7. 产物的原件和元数据为什么分两步写？被接管的旧执行者会留下什么？
8. 为什么子 Agent 不给执行代码和写文件的工具？
9. 4.1 遗留的前端事件过滤问题为什么之前没暴露？

## 8. 已知局限
- 日志里的模型用量来自图状态，崩溃那次执行中“在途”的调用没有计入。
- 被接管的旧执行者可能留下孤儿原件（没有元数据、不可下载），没有清理。
- 审批过期取消时没有发事件（前端刷新后看到“已取消”）。
- 子任务的执行受一个 worker 的并发上限约束；按工具审批（有外部副作用的工具）还没有做。
