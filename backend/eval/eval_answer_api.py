"""端到端回答评测（阶段 2.4，decisions/0018）：经 HTTP 接口走完整的“提问 → Agent 调工具检索 → 带引用回答 → 落库”。

会调用真实模型（按 .env 的 LLM_* 配置），会产生费用：30 个评测问题 + 3 个知识库外的问题，每题一个新对话，
每题通常 2 次模型调用（决定检索、生成回答）。token 用量看 api 日志里的 “llm usage” 行。
用法（服务已用 docker compose 启动）：python eval_answer_api.py [http://localhost:8080] [题数上限]

指标（不用模型打分，只看可客观判断的部分）：
- 检索率：回答前调用了 knowledge_search 的比例；
- 召回：正确答案所在的文档出现在给模型的片段里；
- 引用正确：回答里标注的引用包含正确答案所在的文档；
- 引用精确度：标注的引用中来自正确文档的比例；
- 知识库外的问题：打印回答，人工看是否如实说“没有找到”而不是编造。
"""

import json
import statistics
import sys
import time
import uuid
from pathlib import Path

from eval_search_api import BASE, DATA, call, upload

LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else len(DATA["queries"])
OUT_OF_SCOPE = ["你们支持用比特币付款吗？", "你们公司的 CEO 是谁？", "会员可以免费借用充电宝吗？"]


def ask(h, kb, question):
    cid = call("POST", "/api/conversations", {}, headers=h)["id"]
    call("PUT", f"/api/conversations/{cid}/knowledge-bases", {"knowledge_base_ids": [kb]}, headers=h)
    run = call("POST", f"/api/conversations/{cid}/messages", {"content": question}, headers=h)
    t0 = time.time()
    while True:
        r = call("GET", f"/api/runs/{run['id']}", headers=h)
        if r["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.3)
    elapsed = time.time() - t0
    msgs = call("GET", f"/api/conversations/{cid}/messages", headers=h)
    answer = msgs[-1] if r["status"] == "succeeded" else None
    return r, answer, elapsed


def main():
    user = call("POST", "/api/users", {"name": f"eval-ans-{uuid.uuid4().hex[:8]}"})
    h = {"X-User-Id": user["id"]}
    kb = call("POST", "/api/knowledge-bases", {"name": "回答评测"}, headers=h)["id"]
    for doc_id, text in DATA["docs"].items():
        upload(kb, h, f"{doc_id}.txt", text)
    while not all(d["status"] in ("ready", "failed") for d in call("GET", f"/api/knowledge-bases/{kb}/documents", headers=h)):
        time.sleep(1)

    rows, latencies = [], []
    for q in DATA["queries"][:LIMIT]:
        run, answer, elapsed = ask(h, kb, q["q"])
        latencies.append(elapsed)
        cits = (answer or {}).get("citations") or []
        files = [c["filename"].removesuffix(".txt") for c in cits]
        cited = [c["filename"].removesuffix(".txt") for c in cits if c["cited"]]
        rows.append({
            "q": q["q"], "answer_doc": q["answer"], "status": run["status"], "error_code": run["error_code"],
            "searched": bool(cits), "retrieved": q["answer"] in files, "cited_correct": q["answer"] in cited,
            "cited": cited, "content": (answer or {}).get("content", ""), "seconds": round(elapsed, 1),
        })
        print(f"{'✓' if rows[-1]['cited_correct'] else '✗'} {q['q']}  → 引用 {cited}  {elapsed:.1f}s")

    n = len(rows)
    ok = [r for r in rows if r["status"] == "succeeded"]
    all_cited = [c for r in rows for c in r["cited"]]
    precise = sum(1 for r in rows for c in r["cited"] if c == r["answer_doc"])
    print(f"\n{n} 题：成功 {len(ok)}，检索率 {sum(r['searched'] for r in rows) / n:.2f}，"
          f"召回 {sum(r['retrieved'] for r in rows) / n:.2f}，引用正确 {sum(r['cited_correct'] for r in rows) / n:.2f}，"
          f"引用精确度 {precise / max(1, len(all_cited)):.2f}（共标注 {len(all_cited)} 处），"
          f"未标注引用 {sum(1 for r in rows if r['searched'] and not r['cited'])} 题，"
          f"耗时中位数 {statistics.median(latencies):.1f}s")

    print("\n知识库外的问题（人工检查是否编造）：")
    for question in OUT_OF_SCOPE:
        run, answer, elapsed = ask(h, kb, question)
        cits = (answer or {}).get("citations") or []
        print(f"- {question}（{elapsed:.1f}s，检索 {'是' if cits else '否'}，引用 {[c['n'] for c in cits if c['cited']]}）\n  {(answer or {}).get('content', run['status'])}")

    out = Path(__file__).parent / "results" / f"answer_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"\n逐题结果：{out}")


if __name__ == "__main__":
    main()
