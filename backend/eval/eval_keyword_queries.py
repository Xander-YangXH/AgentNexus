"""精确词查询：现有的“向量召回 + 重排”会不会漏掉？要不要做“关键词 + 向量”的混合检索（阶段 7.5）。

背景：0014 的评测集故意用“换一种说法”的问题（测语义），字二元组 BM25 只有 0.43，所以没做混合检索，
并写明“以后需要精确词（错误码、型号）时再评估”。这里补上那一类：20 个只含精确词的短查询
（错误码、数字 + 单位、专有名词、英文缩写），每个都只在一篇文档里出现。

比较四种：向量（bge-small-zh，与线上一致：不加查询前缀）、向量 + 重排（线上的做法：取前 10 重排）、
字二元组 BM25、RRF 融合（向量与 BM25 各取前 10，按排名融合）后再重排。同时在原来的 30 个“换说法”问题上
检查融合会不会变差。全部本地模型，零费用。

--distractors <目录>：把目录下 Markdown 文件按空行切成段落，作为干扰文档加入语料（例如本项目自己的 docs/，
里面有大量“429”“24 小时”“CSV”这类与查询撞词的技术段落）。语料只有 30 篇时，10 个候选就覆盖了三分之一，
看不出“精确词文档被挤出候选”的问题；加入干扰后才能看出召回阶段的差别。
用法（在 worker 容器里，模型已在镜像中）：
  docker compose cp backend/eval/. worker:/tmp/eval && docker compose cp docs worker:/tmp/eval/docs
  docker compose exec -T -w /tmp/eval worker python eval_keyword_queries.py [--distractors docs]
"""

import argparse
import asyncio
import os
import uuid
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np

from agentnexus.knowledge.embedding import FastEmbedder
from agentnexus.knowledge.rerank import FastReranker
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint

DATA = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())
DOC_IDS = list(DATA["docs"])
DOC_TEXTS = [DATA["docs"][i] for i in DOC_IDS]
TEXT = dict(DATA["docs"])
PARAPHRASE = [(x["q"], x["answer"]) for x in DATA["queries"]]
KEYWORD = [
    ("429", "api_rate_limit"), ("每分钟 600 次", "api_rate_limit"), ("开户行", "invoice_special"),
    ("72 小时", "logistics_tracking"), ("48 小时 拍照", "damaged_package"), ("100 积分", "points_rules"),
    ("22:00", "service_hours"), ("3 期免手续费", "installment"), ("10 万行", "data_export"),
    ("15 天冷静期", "account_deletion"), ("5 万元", "enterprise_purchase"), ("取货码", "store_pickup"),
    ("备用码", "two_factor"), ("CSV", "data_export"), ("Excel", "data_export"), ("港澳台", "overseas_shipping"),
    ("定金", "presale_shipping"), ("五折", "student_discount"), ("秒杀 拼团", "price_protection"),
    ("店铺券 平台券", "coupon_rules"),
]
CANDIDATES = 10  # 与线上 RERANK_CANDIDATES 一致


def grams(t: str) -> list[str]:
    t = "".join(ch for ch in t.lower() if not ch.isspace())
    return [t[i:i + 2] for i in range(len(t) - 1)] or [t]


class BM25:
    def __init__(self, texts, k1=1.5, b=0.75):
        self.tf = [Counter(grams(t)) for t in texts]
        self.avgdl = sum(sum(c.values()) for c in self.tf) / len(self.tf)
        self.df = Counter(g for c in self.tf for g in c)
        self.k1, self.b, self.n = k1, b, len(texts)

    def rank(self, q: str) -> list[str]:
        scores = []
        for tf in self.tf:
            dl, s = sum(tf.values()), 0.0
            for g in set(grams(q)):
                if g in tf:
                    idf = math.log(1 + (self.n - self.df[g] + 0.5) / (self.df[g] + 0.5))
                    s += idf * tf[g] * (self.k1 + 1) / (tf[g] + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            scores.append(s)
        return [DOC_IDS[i] for i in np.argsort(scores, kind="stable")[::-1]]


def rrf(*rankings: list[str], k: int = 60) -> list[str]:
    score: Counter = Counter()
    for r in rankings:
        for pos, d in enumerate(r[:CANDIDATES]):
            score[d] += 1 / (k + pos + 1)
    return [d for d, _ in score.most_common()]


def add_distractors(directory: str) -> int:
    n = 0
    for path in sorted(Path(directory).rglob("*.md")):
        for para in path.read_text().split("\n\n"):
            para = para.strip()
            if 30 <= len(para) <= 400:
                key = f"distractor-{n}"
                DOC_IDS.append(key)
                DOC_TEXTS.append(para)
                TEXT[key] = para
                n += 1
    return n


async def production(embedder, reranker, docs) -> None:
    """用线上的 ChunkIndex（Qdrant 稀疏向量 + 服务端 IDF + RRF）跑同样的查询；临时集合，结束后删除。"""
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
    alias = f"exp_hybrid_{uuid.uuid4().hex[:8]}"
    index, kb = ChunkIndex(client, alias, docs.shape[1]), uuid.uuid4()
    await index.ensure()
    collection = await index.current_collection()
    ids = {uuid.uuid4(): d for d in DOC_IDS}
    await index.upsert([ChunkPoint(cid, kb, cid, docs[DOC_IDS.index(d)].tolist(), TEXT[d]) for cid, d in ids.items()])
    try:
        for name, queries in (("精确词查询（20）", KEYWORD), ("换说法的问题（30）", PARAPHRASE)):
            res = {"稠密（线上旧）": [0, 0, 0], "混合（线上新）": [0, 0, 0]}
            for q, answer in queries:
                qv = embedder.embed_query(q)
                for m, text in (("稠密（线上旧）", None), ("混合（线上新）", q)):
                    cands = [ids[c] for c, _ in await index.search(qv, [kb], limit=CANDIDATES, text=text)]
                    scores = reranker.score(q, [TEXT[d] for d in cands])
                    ranked = [cands[i] for i in np.argsort(scores)[::-1]]
                    res[m][0] += answer in cands
                    res[m][1] += ranked[0] == answer
                    res[m][2] += answer in ranked[:4]
            print(f"\n[线上实现] {name}")
            for m, (pool, top1, top4) in res.items():
                n = len(queries)
                print(f"  {m:<10} 候选召回 {pool / n:.2f}   重排后第一名 {top1 / n:.2f}   前 4 {top4 / n:.2f}")
        import time

        qv = embedder.embed_query("退货要几天")
        for m, text in (("稠密", None), ("混合", "退货要几天")):
            await index.search(qv, [kb], limit=CANDIDATES, text=text)  # 预热
            t = time.perf_counter()
            for _ in range(50):
                await index.search(qv, [kb], limit=CANDIDATES, text=text)
            print(f"  {m}检索耗时：{(time.perf_counter() - t) / 50 * 1000:.1f}ms（50 次平均，{len(DOC_IDS)} 篇）")
    finally:
        from qdrant_client import models

        await client.update_collection_aliases(change_aliases_operations=[
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))])
        await client.delete_collection(collection)
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distractors")
    parser.add_argument("--qdrant", action="store_true", help="同时用线上的 Qdrant 实现跑一遍（需要在 compose 网络里）")
    args = parser.parse_args()
    if args.distractors:
        print(f"加入干扰段落 {add_distractors(args.distractors)} 个，语料共 {len(DOC_IDS)} 篇")
    embedder, reranker, bm25 = FastEmbedder(), FastReranker(), BM25(DOC_TEXTS)
    docs = np.array(embedder.embed_documents(DOC_TEXTS))

    def dense_rank(q):
        return [DOC_IDS[i] for i in np.argsort(-(docs @ np.array(embedder.embed_query(q))))]

    def rerank(q, candidates):
        scores = reranker.score(q, [TEXT[d] for d in candidates])
        return [candidates[i] for i in np.argsort(scores)[::-1]]

    for name, queries in (("精确词查询（20）", KEYWORD), ("换说法的问题（30，0014 的评测集）", PARAPHRASE)):
        hits = {m: [0, 0] for m in ("向量", "向量 + 重排（线上）", "BM25", "RRF 融合 + 重排")}
        pool = {"向量前 10": 0, "RRF 前 10": 0}  # 候选召回：正确答案有没有进入重排的候选
        misses = []
        for q, answer in queries:
            d, b = dense_rank(q), bm25.rank(q)
            pool["向量前 10"] += answer in d[:CANDIDATES]
            pool["RRF 前 10"] += answer in rrf(d, b)[:CANDIDATES]
            ranked = {"向量": d, "向量 + 重排（线上）": rerank(q, d[:CANDIDATES]), "BM25": b,
                      "RRF 融合 + 重排": rerank(q, rrf(d, b)[:CANDIDATES])}
            for m, r in ranked.items():
                hits[m][0] += r[0] == answer
                hits[m][1] += answer in r[:4]  # 线上交给模型的是前 4 个（KNOWLEDGE_TOP_K）
            if ranked["向量 + 重排（线上）"][0] != answer:
                misses.append(f"{q} → {ranked['向量 + 重排（线上）'][0]}（应为 {answer}；"
                              f"候选里{'有' if answer in d[:CANDIDATES] else '没有'}正确答案）")
        print(f"\n{name}")
        for m, (top1, top4) in hits.items():
            print(f"  {m:<14} 第一名命中 {top1 / len(queries):.2f}   前 4 命中 {top4 / len(queries):.2f}")
        print("  候选召回（答案进入重排候选的比例）：" + "，".join(f"{k} {v / len(queries):.2f}" for k, v in pool.items()))
        for line in misses:
            print("  线上漏掉：", line)
    if args.qdrant:
        asyncio.run(production(embedder, reranker, docs))


if __name__ == "__main__":
    main()
