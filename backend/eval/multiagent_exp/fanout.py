"""实验 1：子任务并行（LangGraph Send）——加速比、并发上限，以及进程被 kill -9 后续做时哪些子任务会重做。

对照：同样的子任务写在“一个节点里循环执行”，崩溃后整个节点重做。
用法：BENCH_PG_URL=postgresql://... python fanout.py
"""

import asyncio
import operator
import signal
import subprocess
import sys
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from common import checkpoint_stats, ledger, read_ledger, reset, saver

DURATIONS = {"A": 0.3, "B": 0.3, "C": 3.0, "D": 3.0}  # 模拟子 Agent 的耗时（模型调用 + 工具）


class State(TypedDict):
    topics: list[str]
    results: Annotated[list[str], operator.add]


async def research(state: dict):
    topic = state["topic"]
    ledger("start", topic=topic)
    await asyncio.sleep(state.get("seconds", DURATIONS[topic]))
    ledger("done", topic=topic)
    return {"results": [topic]}


def fanout_graph(cp):
    g = StateGraph(State)
    g.add_node("plan", lambda s: {})
    g.add_node("research", research)
    g.add_node("merge", lambda s: ledger("merge", results=sorted(s["results"])) or {})
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: [Send("research", {"topic": t, **s.get("extra", {})}) for t in s["topics"]],
                            ["research"])
    g.add_edge("research", "merge")
    g.add_edge("merge", END)
    return g.compile(checkpointer=cp)


def loop_graph(cp):
    async def research_all(s: State):
        out = []
        for t in s["topics"]:
            out += (await research({"topic": t}))["results"]
        return {"results": out}

    g = StateGraph(State)
    g.add_node("research_all", research_all)
    g.add_node("merge", lambda s: ledger("merge", results=sorted(s["results"])) or {})
    g.add_edge(START, "research_all")
    g.add_edge("research_all", "merge")
    g.add_edge("merge", END)
    return g.compile(checkpointer=cp)


GRAPHS = {"fanout": fanout_graph, "loop": loop_graph}


async def child(kind: str, thread: str, resume: bool):
    async with saver() as cp:
        graph = GRAPHS[kind](cp)
        cfg = {"configurable": {"thread_id": thread}}
        await graph.ainvoke(None if resume else {"topics": list(DURATIONS), "results": []}, cfg)


async def speed():
    async with saver() as cp:
        for conc in (None, 2):
            t = time.perf_counter()
            cfg = {"configurable": {"thread_id": f"speed-{conc}"}}
            if conc:
                cfg["max_concurrency"] = conc
            await graph_with_seconds(cp, cfg)  # 4 个子任务各 1 秒
            print(f"  4 个 1 秒的子任务，max_concurrency={conc or '不限'}：{time.perf_counter() - t:.2f}s")


async def graph_with_seconds(cp, cfg):
    g = StateGraph(State)
    g.add_node("plan", lambda s: {})

    async def r(state):
        await asyncio.sleep(1.0)
        return {"results": [state["topic"]]}

    g.add_node("research", r)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: [Send("research", {"topic": t}) for t in s["topics"]], ["research"])
    g.add_edge("research", END)
    await g.compile(checkpointer=cp).ainvoke({"topics": ["A", "B", "C", "D"], "results": []}, cfg)


def crash_and_resume(kind: str):
    thread = f"crash-{kind}"
    before = len(read_ledger())
    p = subprocess.Popen([sys.executable, __file__, "child", kind, thread, "start"])
    time.sleep(1.5)  # A、B（0.3 秒）已完成，C、D（3 秒）还在执行
    p.send_signal(signal.SIGKILL)
    p.wait()
    stats = checkpoint_stats(thread)
    subprocess.run([sys.executable, __file__, "child", kind, thread, "resume"], check=True)
    rows = read_ledger()[before:]
    first, second = rows[0]["pid"], [r for r in rows if r["pid"] != rows[0]["pid"]]
    redone = sorted({r["topic"] for r in second if r["event"] == "start"})
    merged = [r["results"] for r in second if r["event"] == "merge"]
    print(f"  {kind}: kill 时 checkpoint={stats}；续做时重新执行的子任务 {redone}；最终汇总 {merged}")


async def main():
    reset()
    print("实验 1a：并行加速")
    await speed()
    print("实验 1b：执行中 kill -9，再用同一个 thread_id 续做（A、B 已完成，C、D 进行中）")
    for kind in ("fanout", "loop"):
        crash_and_resume(kind)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        asyncio.run(child(sys.argv[2], sys.argv[3], sys.argv[4] == "resume"))
    else:
        asyncio.run(main())
