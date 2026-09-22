# LangGraph 学习 Playground

配合 AgentNexus 阶段 0 的学习目标：吃透 StateGraph / checkpoint / interrupt 这三个机制，因为阶段 1 的对话持久化和阶段 4 的崩溃恢复都直接建立在这上面。这是纯练习代码，不是 AgentNexus 的产品代码，随便改随便试。

## 环境准备

```bash
cd learning/langgraph
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

示例 3、4 需要一个能连上的 Postgres。本地起一个最简单：

```bash
docker run -d --name langgraph-learn-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

默认连接串在 `langgraph_learning.py` 顶部的 `PG_URI`，指向 `localhost:5432`，账号密码是 `postgres/postgres`。跟你自己的 Postgres 环境不一样的话改那一行就行。

## 怎么跑

```bash
python langgraph_learning.py 1   # 最基础的 StateGraph：节点、边、状态怎么合并流动
python langgraph_learning.py 2   # 内存 checkpoint：get_state_history 看每一步快照
python langgraph_learning.py 3   # Postgres checkpoint：连续跑两次，感受"状态在数据库里，不在进程里"
python langgraph_learning.py 4   # interrupt 人工审批：第一次跑会暂停，第二次跑模拟审批后恢复
```

**建议顺序**：1 → 2 → 3 → 4，跑完 3 和 4 之后，*务必*去想清楚这个问题——"如果 worker 进程在 interrupt 暂停之后、resume 之前直接被杀掉，重启一个新进程执行同样的代码会发生什么？" 自己动手把这个场景试出来，比看文档记得牢。这也是阶段 4 排队/lease 机制要解决的核心问题的一个简化缩影。

## 每个示例在验证什么

| 示例 | 验证的概念 | 关键 API |
|---|---|---|
| 1 | StateGraph 的基本结构：节点是函数，边定义执行顺序，返回值会合并进 state | `StateGraph` / `add_node` / `add_edge` |
| 2 | checkpoint 不是"最后结果的备份"，是**每一步执行完都存一次快照** | `MemorySaver` / `get_state_history` |
| 3 | checkpoint 换成 Postgres 后，状态跨进程、跨重启依然在 | `PostgresSaver.from_conn_string` / `get_state` |
| 4 | `interrupt()` 会让图执行暂停在某个节点，等外部输入后用 `Command(resume=...)` 继续 | `interrupt` / `Command` |
