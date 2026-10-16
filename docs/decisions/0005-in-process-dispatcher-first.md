# 0005 - 阶段 1 先用进程内派发器，阶段 4 再换 ARQ + lease

**状态**：已决定（阶段 1），阶段 4 将被取代（部分）

**背景**：需要把已落库的 `queued` run 派发出去执行。选项是一开始就上 Redis + ARQ 独立 worker，还是先在 api 进程里用 `asyncio.Queue` 做单机版。

**决定**：
- 阶段 1 用进程内派发器：`submit` 成功后 `enqueue(run_id)`，N 个 worker 协程消费（`WORKER_CONCURRENCY`，默认 4），单个 run 有超时（`RUN_TIMEOUT_SECONDS`，默认 300）。
- **队列不是事实来源**。数据库里的 `queued` 才是：周期性扫描（`SWEEP_INTERVAL_SECONDS`，默认 5）把库里 queued 的 run 补投；队列丢了、进程重启了都不丢任务。
- **正确性靠 `claim` 的原子条件更新**，不靠队列去重。内存里的 `_pending` 集合只是避免重复排队的优化（去掉后测试依然通过，已验证）。
- 启动恢复：把遗留的 `running` 置为 `failed("interrupted: worker restarted")`，对话随之解锁。关停时在途 run 同样收尾为 `failed("interrupted: shutdown")`。
- 先用回声 Agent 打通链路，LLM 在 1.4 接入。

**为什么不一开始就上 ARQ**：ARQ 要解决的是“多 worker 之间的任务分发与存活判定”，这是阶段 4 的问题。现在只有一个进程，先把“落库→派发→抢占→执行→回写→失败收尾”的语义想清楚并测透，再换传输层，换的时候只改派发器，链路语义不变。过早引入分布式组件，会让排错同时面对业务 bug 和分布式 bug 两类问题。

**已知局限（刻意保留，阶段 4 解决）**：
1. **单进程假设**：启动恢复会把所有 `running` 置为 failed。如果同时有两个 api 实例，B 启动时会误杀 A 正在执行的 run。所以 compose 里 api 必须只有 1 个实例、uvicorn 不能开多 `--workers`。lease + heartbeat 就是为了去掉这条假设：只有租约过期的 run 才允许被接管。
2. **失败而不是重试**：被打断的 run 标 failed，由用户重发。有了 LangGraph checkpoint 之后可以改成“从断点恢复”。
3. **kill -9 时不会有收尾**：优雅关停能把在途 run 标记 failed，进程被强杀则只能靠下次启动的恢复。

**后果**：链路在单机上完整可跑、可测；换 ARQ 时 `RunDispatcher` 的对外接口（`enqueue` / `sweep` / `start` / `stop`）可以保持不变。

**验证**：`tests/test_dispatcher.py` 8 个测试 + 变异检查（分别破坏启动恢复、补投、关停收尾、并发上限、超时，对应测试均失败）；容器内端到端：发消息 → run 变 succeeded → 助手回复 `echo: …` 落库；重启 api 后历史仍在、可继续对话。
