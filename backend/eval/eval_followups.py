"""追问的预检索：只用这一句，还是带上上一个问题（阶段 7.6）。

背景：预检索（0018）用用户这一句原话检索。追问往往省略了主题（“那定制的呢”“多久过期”），原话检索可能找错，
模型就得自己再查一次（多一次模型调用），或者拿着不相关的片段回答。

比较三种预检索的查询（都走线上的检索：稠密 + 关键词融合，再重排；看交给模型的前 4 个里有没有正确答案）：
- 只用这一句（现在的做法）；
- 上一个问题 + 这一句（总是拼接）；
- 这一句较短时才拼接（≤ 12 个字）；
- 两路都查、交替合并：这一句和拼接后各检索、各自重排，按“这一句第 1、拼接第 1、这一句第 2……”去重取前 4。
同时放了“换话题”的追问（上一句问退货、这一句问发票），检查拼接会不会把检索带偏。
零费用（本地模型）；用法同 eval_keyword_queries.py（在 worker 容器里，需要 Qdrant）：
  docker compose cp backend/eval/. worker:/tmp/eval && docker compose exec -T -w /tmp/eval worker python eval_followups.py
  加 --distractors docs（先 docker compose cp docs worker:/tmp/eval/docs）用更大的语料
"""

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

import numpy as np
from qdrant_client import AsyncQdrantClient, models

from agentnexus.knowledge.embedding import FastEmbedder
from agentnexus.knowledge.rerank import FastReranker
from agentnexus.storage.vectors import ChunkIndex, ChunkPoint

DATA = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())
SHORT = 12
TOP_K, CANDIDATES = 4, 10  # 与线上一致：KNOWLEDGE_TOP_K、RERANK_CANDIDATES

# (上一个问题, 追问, 追问对应的文档)
FOLLOWUPS = [
    ("退货要几天", "那定制的呢", "return_policy"),
    ("退款多久到账", "用余额付的呢", "refund_timing"),
    ("会员怎么自动续费", "怎么关掉", "cancel_subscription"),
    ("积分怎么用", "多久过期", "points_rules"),
    ("礼品卡能提现吗", "有效期多久", "gift_card"),
    ("学生有优惠吗", "能用几年", "student_discount"),
    ("可以分期付款吗", "手续费怎么算", "installment"),
    ("包裹破了怎么办", "要把坏的寄回去吗", "damaged_package"),
    ("发货了还能改地址吗", "那还没发货呢", "change_address"),
    ("人工客服几点下班", "节假日呢", "service_hours"),
    ("怎么注销账号", "后悔了怎么办", "account_deletion"),
    ("电子产品保修多久", "进水了管吗", "warranty"),
    ("能寄到国外吗", "关税谁出", "overseas_shipping"),
    ("预售的什么时候发货", "定金能退吗", "presale_shipping"),
    ("企业能批量采购吗", "能走对公转账吗", "enterprise_purchase"),
]
# 换话题：拼接上一句可能把检索带偏
SWITCHES = [
    ("退货要几天", "发票怎么开", "invoice_normal"),
    ("积分怎么用", "客服几点下班", "service_hours"),
    ("怎么改收货地址", "密码忘了怎么办", "password_reset"),
    ("学生有优惠吗", "包裹三天没动静了", "logistics_tracking"),
    ("礼品卡能提现吗", "要开专票需要什么", "invoice_special"),
]


def strategies(prev: str, cur: str) -> dict[str, str]:
    return {"只用这一句（现在）": cur, "总是拼接上一句": f"{prev} {cur}",
            f"短句（≤{SHORT} 字）才拼接": f"{prev} {cur}" if len(cur) <= SHORT else cur}


def distractors(directory: str) -> dict[str, str]:
    """与 eval_keyword_queries.py 相同：Markdown 按空行切段，30~400 字的段落作为干扰。"""
    out, n = {}, 0
    for path in sorted(Path(directory).rglob("*.md")):
        for para in path.read_text().split("\n\n"):
            para = para.strip()
            if 30 <= len(para) <= 400:
                out[f"distractor-{n}"] = para
                n += 1
    return out


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distractors")
    args = parser.parse_args()
    if args.distractors:
        extra = distractors(args.distractors)
        DATA["docs"].update(extra)
        print(f"加入干扰段落 {len(extra)} 个，语料共 {len(DATA['docs'])} 篇")
    embedder, reranker = FastEmbedder(), FastReranker()
    client = AsyncQdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
    alias, kb = f"exp_followup_{uuid.uuid4().hex[:8]}", uuid.uuid4()
    index = ChunkIndex(client, alias, embedder.dim)
    await index.ensure()
    collection = await index.current_collection()
    ids = {uuid.uuid4(): d for d in DATA["docs"]}
    vectors = embedder.embed_documents([DATA["docs"][d] for d in ids.values()])
    await index.upsert([ChunkPoint(c, kb, c, v, DATA["docs"][d]) for (c, d), v in zip(ids.items(), vectors, strict=True)])
    try:
        for name, cases in (("追问（15）", FOLLOWUPS), ("换话题（5）", SWITCHES)):
            hits: dict[str, list[int]] = {}
            for prev, cur, answer in cases:
                results = {}
                for label, query in strategies(prev, cur).items():
                    cands = [ids[c] for c, _ in await index.search(embedder.embed_query(query), [kb], limit=CANDIDATES,
                                                                    text=query)]
                    # 重排用的是什么问题：拼接时也用拼接后的（线上预检索只有这一个查询）
                    scores = reranker.score(query, [DATA["docs"][d] for d in cands])
                    results[label] = [cands[i] for i in np.argsort(scores)[::-1]]
                raw, joined = results["只用这一句（现在）"], results["总是拼接上一句"]
                results["两路交替合并"] = list(dict.fromkeys(d for pair in zip(raw, joined, strict=False) for d in pair))
                for label, ranked in results.items():
                    h = hits.setdefault(label, [0, 0])
                    h[0] += ranked[0] == answer
                    h[1] += answer in ranked[:TOP_K]
            print(f"\n{name}")
            for label, (top1, topk) in hits.items():
                print(f"  {label:<16} 第一名 {top1 / len(cases):.2f}   前 {TOP_K} {topk / len(cases):.2f}")
    finally:
        await client.update_collection_aliases(change_aliases_operations=[
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))])
        await client.delete_collection(collection)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
