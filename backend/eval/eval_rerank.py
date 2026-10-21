"""在 embedding 召回的基础上加重排（cross-encoder），看能否把正确答案提到第一（结果见 decisions/0014）。

用法：python eval_rerank.py（依赖 eval_embeddings.py 同目录的数据与函数；不调用付费 API）
"""

import time

import numpy as np
from fastembed.rerank.cross_encoder import TextCrossEncoder

from eval_embeddings import DATA, DOC_IDS, QUERIES, dense, metrics

RECALL_MODEL = "BAAI/bge-small-zh-v1.5"
RERANKER = "BAAI/bge-reranker-v2-m3-int8"
TOP_K = 10


def main():
    recall_rankings, _ = dense(RECALL_MODEL, None)
    base = metrics(recall_rankings)
    reranker = TextCrossEncoder(RERANKER)
    list(reranker.rerank("预热", ["预热"]))
    reranked, times = [], []
    for (q, _), ranked in zip(QUERIES, recall_rankings):
        candidates = ranked[:TOP_K]
        t0 = time.perf_counter()
        scores = list(reranker.rerank(q, [DATA["docs"][d] for d in candidates]))
        times.append((time.perf_counter() - t0) * 1000)
        order = [candidates[i] for i in np.argsort(scores)[::-1]]
        reranked.append(order + ranked[TOP_K:])
    after = metrics(reranked)
    print(f"召回 {RECALL_MODEL}：R@1 {base['R@1']:.2f}  R@3 {base['R@3']:.2f}  MRR {base['MRR']:.2f}")
    print(f"+ 重排 {RERANKER}（前 {TOP_K} 名）：R@1 {after['R@1']:.2f}  R@3 {after['R@3']:.2f}  MRR {after['MRR']:.2f}")
    print(f"重排耗时：每个问题 {np.median(times):.0f}ms（中位数），{max(times):.0f}ms（最大）")
    fixed = {q for q, *_ in base["misses"]} - {q for q, *_ in after["misses"]}
    broken = {q for q, *_ in after["misses"]} - {q for q, *_ in base["misses"]}
    print("重排后改对的：", sorted(fixed))
    print("重排后改错的：", sorted(broken))


if __name__ == "__main__":
    main()
