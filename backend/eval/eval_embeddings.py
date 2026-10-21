"""在自写的中文客服检索评测集上比较 embedding 模型（阶段 2 选型用，结果见 decisions/0014）。

用法：pip install fastembed numpy；python eval_embeddings.py
不调用任何付费 API：模型通过 fastembed 在本地 CPU 上运行（首次运行会下载模型文件）。
"""

import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

DATA = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())
DOC_IDS = list(DATA["docs"])
DOC_TEXTS = [DATA["docs"][i] for i in DOC_IDS]
QUERIES = [(x["q"], x["answer"]) for x in DATA["queries"]]

# (模型, 官方推荐的查询前缀)。文档侧都不加前缀
MODELS = [
    ("BAAI/bge-small-zh-v1.5", "为这个句子生成表示以用于检索相关文章："),
    ("jinaai/jina-embeddings-v2-base-zh", None),
    ("Qwen/Qwen3-Embedding-0.6B-Q", "Instruct: Given a user question, retrieve the knowledge base article that answers it\nQuery:"),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", None),
]


def metrics(rankings: list[list[str]]) -> dict:
    """rankings[i] 是第 i 个问题的文档排序。"""
    r1 = r3 = mrr = 0.0
    misses = []
    for (q, answer), ranked in zip(QUERIES, rankings):
        pos = ranked.index(answer)
        r1 += pos == 0
        r3 += pos < 3
        mrr += 1 / (pos + 1)
        if pos != 0:
            misses.append((q, answer, ranked[0], pos + 1))
    n = len(QUERIES)
    return {"R@1": r1 / n, "R@3": r3 / n, "MRR": mrr / n, "misses": misses}


def bm25_char_bigram(k1: float = 1.5, b: float = 0.75) -> list[list[str]]:
    """关键词基线：中文按字二元组切分的 BM25（不做分词）。"""
    def grams(t):
        t = "".join(ch for ch in t if not ch.isspace())
        return [t[i:i + 2] for i in range(len(t) - 1)]
    doc_tf = [Counter(grams(t)) for t in DOC_TEXTS]
    avgdl = sum(sum(c.values()) for c in doc_tf) / len(doc_tf)
    df = Counter(g for c in doc_tf for g in c)
    n = len(doc_tf)
    rankings = []
    for q, _ in QUERIES:
        scores = []
        for i, tf in enumerate(doc_tf):
            dl = sum(tf.values())
            s = 0.0
            for g in set(grams(q)):
                if g in tf:
                    idf = math.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5))
                    s += idf * tf[g] * (k1 + 1) / (tf[g] + k1 * (1 - b + b * dl / avgdl))
            scores.append(s)
        rankings.append([DOC_IDS[i] for i in np.argsort(scores)[::-1]])
    return rankings


def dense(model_name: str, query_prefix: str | None) -> tuple[list[list[str]], dict]:
    model = TextEmbedding(model_name)
    list(model.embed(["预热"]))  # 首次推理有初始化开销，不计入
    t0 = time.perf_counter()
    d = np.array(list(model.embed(DOC_TEXTS)))
    doc_ms = (time.perf_counter() - t0) * 1000 / len(DOC_TEXTS)
    qs = [(query_prefix or "") + q for q, _ in QUERIES]
    t0 = time.perf_counter()
    for q in qs[:10]:
        list(model.embed([q]))  # 线上是一次一个查询，单条计时更贴近实际
    q_ms = (time.perf_counter() - t0) * 1000 / 10
    qv = np.array(list(model.embed(qs)))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    qv /= np.linalg.norm(qv, axis=1, keepdims=True)
    sims = qv @ d.T
    rankings = [[DOC_IDS[i] for i in np.argsort(-row)] for row in sims]
    return rankings, {"doc_ms": doc_ms, "query_ms": q_ms, "dim": d.shape[1]}


def main():
    rows = []
    m = metrics(bm25_char_bigram())
    rows.append(("关键词基线：字二元组 BM25", "-", m, None))
    for name, prefix in MODELS:
        variants = [("不加前缀", None)] + ([("加推荐前缀", prefix)] if prefix else [])
        for label, p in variants:
            rankings, perf = dense(name, p)
            rows.append((name, label, metrics(rankings), perf))
    print(f"{'方法':58} {'查询前缀':10} {'R@1':>5} {'R@3':>5} {'MRR':>5} {'维度':>5} {'文档ms/篇':>9} {'查询ms':>7}")
    for name, label, m, perf in rows:
        p = f"{perf['dim']:>5} {perf['doc_ms']:>9.1f} {perf['query_ms']:>7.1f}" if perf else f"{'-':>5} {'-':>9} {'-':>7}"
        print(f"{name:58} {label:10} {m['R@1']:>5.2f} {m['R@3']:>5.2f} {m['MRR']:>5.2f} {p}")
    print("\n各方法排第一却答错的问题（问题 | 正确答案 → 排第一的 | 正确答案排名）:")
    for name, label, m, _ in rows:
        if m["misses"]:
            print(f"  [{name.split('/')[-1]} {label}]")
            for q, ans, top, pos in m["misses"]:
                print(f"     {q} | {ans} → {top} | 第 {pos}")


if __name__ == "__main__":
    main()
