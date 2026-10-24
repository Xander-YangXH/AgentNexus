"""分块策略的检索评测：把 30 篇知识条目合成一个 Markdown 文件（每篇一个二级标题）上传，看检索第一名是否包含正确条目的正文。

和 eval_search_api.py 的区别：那里每篇一个文件，分块不会把不同条目混在一起；这里测的是“长文档被切块”之后的检索效果，
用来判断要不要做“按标题切分”。不调用付费 API。用法：python eval_chunking_md.py [http://localhost:8080]
"""

import time

from eval_search_api import DATA, call, eval_session, upload


def main():
    h = eval_session("eval-md")
    kb = call("POST", "/api/knowledge-bases", {"name": "分块评测"}, headers=h)["id"]
    md = "\n\n".join(f"## {doc_id}\n{text}" for doc_id, text in DATA["docs"].items())
    upload(kb, h, "客服手册.md", md)
    while (docs := call("GET", f"/api/knowledge-bases/{kb}/documents", headers=h))[0]["status"] not in ("ready", "failed"):
        time.sleep(0.5)
    chunks = call("GET", f"/api/documents/{docs[0]['id']}/chunks", headers=h)
    print(f"1 个文件 → {len(chunks)} 块，平均 {sum(c['token_count'] for c in chunks) / len(chunks):.0f} token")
    for rerank in (False, True):
        hit1 = hit3 = 0
        for q in DATA["queries"]:
            res = call("POST", "/api/knowledge/search",
                       {"knowledge_base_ids": [kb], "query": q["q"], "top_k": 3, "rerank": rerank}, headers=h)
            key = DATA["docs"][q["answer"]][:20]  # 正确条目正文的开头：出现在块里就算包含了答案
            found = [key in hit["text"] for hit in res["hits"]]
            hit1 += bool(found[:1] and found[0])
            hit3 += any(found)
        n = len(DATA["queries"])
        print(f"{'重排' if rerank else '仅召回'}：第一名包含答案 {hit1 / n:.2f}，前三名包含答案 {hit3 / n:.2f}")
    call("DELETE", f"/api/knowledge-bases/{kb}")  # 评测数据用完即删（decisions/0026）


if __name__ == "__main__":
    main()
