"""端到端检索评测：通过 HTTP 接口走完整链路（上传 → 分块 → 向量化 → Qdrant → 回库 → 重排），用自建评测集算命中率。

和 eval_embeddings.py / eval_rerank.py 的区别：那两个直接调模型，这个测的是部署好的系统本身（含分块、过滤、回库）。
不调用任何付费 API。用法（服务已用 docker compose 启动）：
    python eval_search_api.py [http://localhost:8080]
每次运行新建一个用户和知识库，不影响已有数据。
"""

import json
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
DATA = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())


def call(method, path, body=None, headers=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={**(headers or {}), "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"null")


def upload(kb, h, name, text):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n").encode() + text.encode() + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", f"/api/knowledge-bases/{kb}/documents", headers=h, raw=body,
                content_type=f"multipart/form-data; boundary={boundary}")


def main():
    user = call("POST", "/api/users", {"name": f"eval-{uuid.uuid4().hex[:8]}"})
    h = {"X-User-Id": user["id"]}
    kb = call("POST", "/api/knowledge-bases", {"name": "检索评测"}, headers=h)["id"]
    for doc_id, text in DATA["docs"].items():  # 一篇知识条目一个文件，文件名就是答案 id
        upload(kb, h, f"{doc_id}.txt", text)
    t0 = time.time()
    while True:
        docs = call("GET", f"/api/knowledge-bases/{kb}/documents", headers=h)
        if all(d["status"] in ("ready", "failed") for d in docs):
            break
        time.sleep(1)
    failed = [d["filename"] for d in docs if d["status"] == "failed"]
    print(f"入库 {len(docs)} 篇，{sum(d['chunk_count'] for d in docs)} 块，用时 {time.time() - t0:.1f}s，失败 {failed}")

    for rerank in (False, True):
        r1 = r3 = mrr = 0.0
        latency, steps = [], {}
        for q in DATA["queries"]:
            t = time.perf_counter()
            res = call("POST", "/api/knowledge/search",
                       {"knowledge_base_ids": [kb], "query": q["q"], "top_k": 10, "rerank": rerank}, headers=h)
            latency.append((time.perf_counter() - t) * 1000)
            for k, v in res["timings_ms"].items():
                steps.setdefault(k, []).append(v)
            ranked = list(dict.fromkeys(hit["filename"].removesuffix(".txt") for hit in res["hits"]))
            if q["answer"] in ranked:
                pos = ranked.index(q["answer"])
                r1 += pos == 0
                r3 += pos < 3
                mrr += 1 / (pos + 1)
        n = len(DATA["queries"])
        step_txt = "，".join(f"{k} {statistics.median(v):.0f}ms" for k, v in steps.items())
        print(f"{'重排' if rerank else '仅向量'}：R@1 {r1 / n:.2f}  R@3 {r3 / n:.2f}  MRR {mrr / n:.2f}  "
              f"接口耗时中位数 {statistics.median(latency):.0f}ms（{step_txt}）")


if __name__ == "__main__":
    main()
