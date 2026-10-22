"""实验 2：审批中断（LangGraph interrupt / Command(resume)）。

问题：
1. 中断后进程退出，另一个进程只凭 checkpoint 能不能接着执行？等待期间占多少存储？
2. 恢复时，节点从头重新执行——interrupt() 之前的代码（例如一次模型调用）会不会再跑一遍？
3. 拒绝 + 修改意见：能不能回到“制定计划”重新来？
用法：BENCH_PG_URL=postgresql://... python interrupt_exp.py
"""

import asyncio
import os
import subprocess
import sys
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common import checkpoint_stats, ledger, read_ledger, reset, saver


class State(TypedDict, total=False):
    request: str
    plan: list[str]
    feedback: str
    approved: bool
    report: str


def model_plan(state: State) -> list[str]:
    ledger("model_call", purpose="plan", feedback=state.get("feedback"))  # 有成本的一步
    # 真实模型每次输出都可能不同：用进程号模拟“同样的输入，这次生成的计划不一样”
    tag = os.getpid() % 1000
    return [f"A{tag}", f"B{tag}", f"C{tag}"] if not state.get("feedback") else [f"A{tag}", f"B{tag}"]


def separate(cp):
    """推荐写法：调用模型和等待审批分成两个节点。"""
    def plan(state: State):
        return {"plan": model_plan(state)}

    def approve(state: State):
        ledger("approve_node_enter")
        decision = interrupt({"plan": state["plan"]})
        return {"approved": decision["approved"], "feedback": decision.get("feedback", "")}

    def execute(state: State):
        ledger("execute", plan=state["plan"])
        return {"report": "+".join(state["plan"])}

    g = StateGraph(State)
    g.add_node("plan", plan)
    g.add_node("approve", approve)
    g.add_node("execute", execute)
    g.add_edge(START, "plan")
    g.add_edge("plan", "approve")
    g.add_conditional_edges("approve", lambda s: "execute" if s["approved"] else "plan", ["execute", "plan"])
    g.add_edge("execute", END)
    return g.compile(checkpointer=cp)


def combined(cp):
    """反例：模型调用和 interrupt() 写在同一个节点里。"""
    def plan_and_approve(state: State):
        plan = model_plan(state)
        decision = interrupt({"plan": plan})
        return {"plan": plan, "approved": decision["approved"]}

    def execute(state: State):
        ledger("execute", plan=state["plan"])
        return {"report": "+".join(state["plan"])}

    g = StateGraph(State)
    g.add_node("plan_and_approve", plan_and_approve)
    g.add_node("execute", execute)
    g.add_edge(START, "plan_and_approve")
    g.add_edge("plan_and_approve", "execute")
    g.add_edge("execute", END)
    return g.compile(checkpointer=cp)


GRAPHS = {"separate": separate, "combined": combined}


async def step(kind: str, thread: str, action: str):
    """每一步都在一个新进程里执行：模拟“提交 → worker 执行到中断后释放 → 用户批准 → 另一个 worker 恢复”。"""
    async with saver() as cp:
        graph = GRAPHS[kind](cp)
        cfg = {"configurable": {"thread_id": thread}}
        if action == "start":
            out = await graph.ainvoke({"request": "调研三个主题"}, cfg)
        elif action == "approve":
            out = await graph.ainvoke(Command(resume={"approved": True}), cfg)
        else:  # reject
            out = await graph.ainvoke(Command(resume={"approved": False, "feedback": "只要 A、B"}), cfg)
        snap = await graph.aget_state(cfg)
        waiting = [i.value for t in snap.tasks for i in t.interrupts]
        print(f"    [{action}] next={list(snap.next)} waiting_for={waiting} report={out.get('report')}")


def run(kind: str, thread: str, *actions: str):
    before = len(read_ledger())
    for a in actions:
        subprocess.run([sys.executable, __file__, "step", kind, thread, a], check=True)
        if a == "start":
            print(f"    等待审批时的存储：{checkpoint_stats(thread)}")
    rows = read_ledger()[before:]
    calls = [r for r in rows if r["event"] == "model_call"]
    print(f"    模型调用 {len(calls)} 次（{len({r['pid'] for r in calls})} 个进程）；"
          f"审批节点进入 {sum(r['event'] == 'approve_node_enter' for r in rows)} 次；"
          f"执行 {[r['plan'] for r in rows if r['event'] == 'execute']}")


def main():
    reset()
    print("2a 推荐写法（计划、审批分两个节点）：开始 → 批准，每步一个新进程")
    run("separate", "t-separate", "start", "approve")
    print("2b 反例（模型调用和 interrupt 在同一节点）：开始 → 批准")
    run("combined", "t-combined", "start", "approve")
    print("2c 拒绝并给修改意见 → 重新制定计划 → 批准")
    run("separate", "t-reject", "start", "reject", "approve")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "step":
        asyncio.run(step(*sys.argv[2:5]))
    else:
        main()
