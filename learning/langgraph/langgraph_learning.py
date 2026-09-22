"""
LangGraph 学习示例 —— 配合 AgentNexus 阶段 0 学习内容食用

运行方式：
    python langgraph_learning.py <示例编号>

示例列表：
    1 - 最基础的 StateGraph：节点、边、状态怎么合并流动（不需要 Postgres）
    2 - 内存 checkpoint：用 get_state_history 看每一步执行完存的快照（不需要 Postgres）
    3 - Postgres checkpoint：同一个 thread_id 连续跑两次，感受"状态存在数据库里，不在进程里"
    4 - interrupt 人工审批 + 恢复：第一次跑会暂停在审批节点，再跑一次模拟审批通过后继续执行

示例 3、4 需要一个能连上的 Postgres，见下面 PG_URI（默认 localhost:5432，账号密码 postgres/postgres）。
本地没有现成 Postgres 的话，先起一个：
    docker run -d --name langgraph-learn-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
"""

import sys
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

PG_URI = "postgresql://postgres:postgres@localhost:5432/postgres?sslmode=disable"


class State(TypedDict):
    messages: Annotated[list, add_messages]
    count: int


def node_a(state: State):
    print(f"[node_a] 执行，进入时 count={state['count']}")
    return {"count": state["count"] + 1, "messages": [("ai", "node_a 跑完了")]}


def node_b(state: State):
    print(f"[node_b] 执行，进入时 count={state['count']}")
    return {"count": state["count"] + 10, "messages": [("ai", "node_b 跑完了")]}


def build_basic_graph() -> StateGraph:
    g = StateGraph(State)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")
    g.add_edge("node_b", END)
    return g


# ---------- 示例 1：最基础的 StateGraph ----------
def example_1():
    """不接 checkpointer，纯跑一遍，感受 state 在节点间怎么合并、怎么流动。"""
    graph = build_basic_graph().compile()
    result = graph.invoke({"messages": [], "count": 0})
    print("\n最终状态：", result)


# ---------- 示例 2：内存 checkpoint ----------
def example_2():
    """
    接上 MemorySaver，跑完之后用 get_state_history 把每一步的快照都打出来，
    体会"checkpoint 不是最后结果的备份，是每一步都在存"。
    """
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    graph = build_basic_graph().compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "demo-memory-1"}}
    graph.invoke({"messages": [], "count": 0}, config)

    print("\n--- 历史快照（越往下越早）---")
    for snap in graph.get_state_history(config):
        print(f"step={snap.metadata.get('step')} count={snap.values.get('count')} next={snap.next}")


# ---------- 示例 3：Postgres checkpoint，跨进程恢复 ----------
def example_3():
    """
    每次运行这个脚本都是一个全新的 Python 进程，但用的是同一个 thread_id。
    第一次跑：图执行完，状态存进 Postgres。
    再跑一次：不会重新从头执行，直接能读到上次存的最终状态——
    这就是"进程重启、状态还在"的直观感受，也是阶段 1 对话持久化的原型。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(PG_URI) as checkpointer:
        checkpointer.setup()  # 第一次运行会自动建表，重复调用是安全的
        graph = build_basic_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "demo-postgres-1"}}

        existing = graph.get_state(config)
        if existing.values:
            print(f"\n发现已有状态（来自上一次进程的运行）：{existing.values}")
            print("这就是刚才说的关键点：这个状态是从 Postgres 读出来的，不是这次进程重新算出来的。")
        else:
            print("\n没有历史状态，这是第一次运行，开始执行图：")
            result = graph.invoke({"messages": [], "count": 0}, config)
            print("执行完毕，已写入 Postgres：", result)
            print("\n再运行一次 `python langgraph_learning.py 3`，观察上面会走哪个分支。")


# ---------- 示例 4：interrupt 人工审批 + 恢复 ----------
def node_risky_action(state: State):
    from langgraph.types import interrupt

    decision = interrupt({
        "question": "要不要批准这个有风险的操作？",
        "当前 count": state["count"],
    })
    if decision == "approve":
        return {"count": state["count"] + 100, "messages": [("ai", "已批准，count +100")]}
    return {"count": state["count"], "messages": [("ai", "被拒绝，count 不变")]}


def build_approval_graph() -> StateGraph:
    g = StateGraph(State)
    g.add_node("node_a", node_a)
    g.add_node("risky", node_risky_action)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "risky")
    g.add_edge("risky", END)
    return g


def example_4():
    """
    第一次运行：图跑到 risky 节点会暂停（interrupt），这时候看到的是一个
    "待批准的请求"，而不是最终结果——模拟一个长任务卡在等人工审批的状态。
    第二次运行同一个命令：不会重新问一遍，而是把上次暂停的请求当成
    "已批准"去恢复执行——模拟"审批通过后，一个全新进程把任务继续跑完"。
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command

    with PostgresSaver.from_conn_string(PG_URI) as checkpointer:
        checkpointer.setup()
        graph = build_approval_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "demo-interrupt-1"}}

        state = graph.get_state(config)
        if state.next:
            print(f"\n发现有一个暂停中的任务，等待审批：{state.tasks}")
            print("模拟人工审批通过，调用 Command(resume='approve') 继续执行：")
            result = graph.invoke(Command(resume="approve"), config)
            print("恢复执行后的最终状态：", result)
            print("\n任务已经跑完。想重新体验一次暂停效果，换一个 thread_id（改代码里的字符串）就行。")
        else:
            result = graph.invoke({"messages": [], "count": 0}, config)
            if "__interrupt__" in result:
                print(f"\n图执行到 risky 节点暂停了，等待审批：{result['__interrupt__']}")
                print("再运行一次 `python langgraph_learning.py 4`，这次会自动模拟审批通过并恢复执行。")
            else:
                print("\n图跑完了（没有触发暂停），结果：", result)


EXAMPLES = {"1": example_1, "2": example_2, "3": example_3, "4": example_4}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in EXAMPLES:
        print(__doc__)
        sys.exit(1)
    EXAMPLES[sys.argv[1]]()
