"""端到端检索评测：通过 HTTP 接口走完整链路（上传 → 分块 → 向量化 → Qdrant → 回库 → 重排），用自建评测集算命中率。

和 eval_embeddings.py / eval_rerank.py 的区别：那两个直接调模型，这个测的是部署好的系统本身（含分块、过滤、回库）。
不调用任何付费 API。用法（服务已用 docker compose 启动）：
    ADMIN_PASSWORD=... python eval_search_api.py [http://localhost:8080]
每次运行用管理员账号新建一个一次性的评测用户和知识库，结束时删除知识库，不影响已有数据。

阶段 5 起接口要登录（会话 Cookie + CSRF 的 Origin 校验），这几个脚本原来用的 X-User-Id 头和 POST /api/users
已经不存在了，阶段 7.7 补全前它们一直跑不通——这里的 call / eval_session 由三个评测脚本共用。
"""

import http.cookiejar
import json
import os
import secrets
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
DATA = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())


_cookies = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))


def call(method, path, body=None, headers=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    # Origin：写请求要过 CSRF 检查（与浏览器一样）；会话 Cookie 由 _opener 保存和带上
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={**(headers or {}), "Content-Type": content_type, "Origin": BASE})
    with _opener.open(req, timeout=120) as r:
        return json.loads(r.read() or b"null")


def eval_session(prefix: str) -> dict:
    """用管理员账号（ADMIN_NAME / ADMIN_PASSWORD）建一个一次性的评测用户，并以它登录。返回空的 headers（兼容旧调用）。"""
    admin_pw = os.environ.get("ADMIN_PASSWORD") or sys.exit("set ADMIN_PASSWORD (the admin account creates a throwaway eval user)")
    call("POST", "/api/auth/login", {"name": os.environ.get("ADMIN_NAME", "admin"), "password": admin_pw})
    name, password = f"{prefix}-{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(16)
    call("POST", "/api/admin/users", {"name": name, "password": password, "role": "member"})
    _cookies.clear()
    call("POST", "/api/auth/login", {"name": name, "password": password})
    return {}


def upload(kb, h, name, text):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n").encode() + text.encode() + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", f"/api/knowledge-bases/{kb}/documents", headers=h, raw=body,
                content_type=f"multipart/form-data; boundary={boundary}")


def main():
    h = eval_session("eval-search")
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
        print(f"{'重排' if rerank else '仅召回'}：R@1 {r1 / n:.2f}  R@3 {r3 / n:.2f}  MRR {mrr / n:.2f}  "
              f"接口耗时中位数 {statistics.median(latency):.0f}ms（{step_txt}）")
    call("DELETE", f"/api/knowledge-bases/{kb}")  # 评测数据用完即删（decisions/0026）


if __name__ == "__main__":
    main()
