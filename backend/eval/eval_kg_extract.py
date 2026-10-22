"""知识图谱选型实验（decisions/0019 草稿）：用 LLM 从分块中抽取实体与关系，测质量、成本、耗时。

两份语料：
- customer：评测集的 30 篇客服条目（合成一个 Markdown，按标题切成 30 块）——短小、规则类；
- decisions：本项目自己的 5 篇决策记录（按标题切块）——技术文档，实体多为技术与组件。
会调用真实模型（读取仓库根目录 .env 的 LLM_*），产生少量费用。用法：
    python eval_kg_extract.py [customer|decisions|all] [v1|v2]
- v1：选型时的第 1 版提示词与 schema（定义在本文件里，保留作对照）；
- v2：正式代码里的抽取器（knowledge/graph/extraction.py，收紧了实体粒度与数量）。
输出：每块的抽取结果与用量写到 results/kg_extract_<语料>_<版本>.json，汇总打印到终端，供人工抽样核对。
"""

import asyncio
import json
import statistics
import sys
import time
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "package"))

from agentnexus.knowledge.chunking import chunk_sections  # noqa: E402
from agentnexus.knowledge.parsing import Section  # noqa: E402
from agentnexus.knowledge.tokens import EstimatingCounter  # noqa: E402

EntityType = Literal["产品服务", "规则政策", "流程操作", "组织角色", "渠道地点", "期限数量", "技术组件", "概念术语"]


class Entity(BaseModel):
    name: str = Field(description="实体的规范名称，简短（一般不超过 12 个字），不带修饰语")
    type: EntityType
    description: str = Field(description="一句话说明，不超过 40 个字，只依据原文")


class Relation(BaseModel):
    source: str = Field(description="起点实体名，必须出现在 entities 里")
    target: str = Field(description="终点实体名，必须出现在 entities 里")
    relation: str = Field(description="关系谓词，2~6 个字，如“适用于”“依赖”“替代”“需要”“不支持”")
    description: str = Field(description="一句话说明这条关系，不超过 40 个字，只依据原文")


class Extraction(BaseModel):
    """从一段知识库文本中抽取的实体与关系。"""

    entities: list[Entity]
    relations: list[Relation]


PROMPT = """从下面的知识库片段中抽取实体和实体之间的关系，用于构建知识图谱。
要求：
- 只抽取原文明确提到的内容，不要推测、不要补充常识。
- 实体是值得在图谱里单独成为节点的东西（产品、规则、流程、角色、技术、期限等）；普通形容词、整句话不是实体。
- 同一个东西用同一个名字（例如“无理由退货”不要一会儿写成“七天无理由退货”）。
- 关系要有方向：source 做了什么 / 对 target 有什么约束。两端都必须是你列出的实体。
- 片段里没有值得抽取的内容时，返回空列表。

片段（来自《{title}》）：
{text}"""


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def corpus(name):
    if name == "customer":
        data = json.loads((Path(__file__).parent / "retrieval_zh_v0.json").read_text())
        md = "\n\n".join(f"## {k}\n{v}" for k, v in data["docs"].items())
        return [("客服手册", c.text) for c in chunk_sections([Section(md)], EstimatingCounter(), heading_boundaries=True)]
    out = []
    for f in ["0005", "0007", "0009", "0014", "0016"]:
        path = next((ROOT / "docs" / "decisions").glob(f"{f}-*.md"))
        title = path.read_text().splitlines()[0].lstrip("# ")
        out += [(title, c.text) for c in chunk_sections([Section(path.read_text())], EstimatingCounter(), heading_boundaries=True)]
    return out


def norm(name: str) -> str:
    """实体名规范化：全角转半角、去空白、英文小写——合并时用。"""
    return "".join(unicodedata.normalize("NFKC", name).split()).lower()


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    version = sys.argv[2] if len(sys.argv) > 2 else "v2"
    env = load_env()
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=env["LLM_MODEL"], api_key=env["LLM_API_KEY"], base_url=env.get("LLM_BASE_URL"),
                        thinking={"type": "disabled"}, max_tokens=2048, temperature=0)
    extractor = llm.with_structured_output(Extraction, include_raw=True)
    sem = asyncio.Semaphore(4)
    if version == "v2":
        from agentnexus.knowledge.graph.extraction import LLMGraphExtractor

        production = LLMGraphExtractor(llm)

    async def one(i, title, text):
        if version == "v2":
            async with sem:
                t0 = time.perf_counter()
                r = await production.extract(title, text)
            return {"i": i, "title": title, "text": text, "seconds": round(time.perf_counter() - t0, 2), "attempts": 1,
                    "failed": False, "input": r.input_tokens, "cache_read": r.cached_tokens, "output": r.output_tokens,
                    "entities": r.payload["entities"], "relations": r.payload["relations"], "dangling": 0}
        async with sem:
            t0 = time.perf_counter()
            for attempt in (1, 2):  # 解析失败重试一次
                res = await extractor.ainvoke(PROMPT.format(title=title, text=text))
                if res["parsed"] is not None:
                    break
            usage = res["raw"].usage_metadata or {}
            ext = res["parsed"] or Extraction(entities=[], relations=[])
            names = {e.name for e in ext.entities}
            dangling = [r for r in ext.relations if r.source not in names or r.target not in names]
            return {"i": i, "title": title, "text": text, "seconds": round(time.perf_counter() - t0, 2), "attempts": attempt,
                    "failed": res["parsed"] is None, "input": usage.get("input_tokens", 0),
                    "cache_read": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
                    "output": usage.get("output_tokens", 0),
                    "entities": [e.model_dump() for e in ext.entities],
                    "relations": [r.model_dump() for r in ext.relations if r not in dangling],
                    "dangling": len(dangling)}

    for name in (["customer", "decisions"] if which == "all" else [which]):
        chunks = corpus(name)
        t0 = time.perf_counter()
        rows = await asyncio.gather(*(one(i, t, x) for i, (t, x) in enumerate(chunks)))
        wall = time.perf_counter() - t0
        inp, cache, out = (sum(r[k] for r in rows) for k in ("input", "cache_read", "output"))
        cost = cache * 0.006e-6 + (inp - cache) * 0.3e-6 + out * 1.2e-6  # decisions/0013 的高峰价（美元/token）
        ents = [e for r in rows for e in r["entities"]]
        rels = [x for r in rows for x in r["relations"]]
        merged = {}
        for e in ents:
            merged.setdefault(norm(e["name"]), set()).add(e["type"])
        print(f"\n== {name}（{version}）：{len(chunks)} 块，并发 4，总耗时 {wall:.1f}s，单块中位数 {statistics.median(r['seconds'] for r in rows):.1f}s")
        print(f"   token：输入 {inp}（缓存 {cache}）输出 {out}；费用 ${cost:.4f}，每块 ${cost / len(chunks):.5f}")
        print(f"   解析失败 {sum(r['failed'] for r in rows)}，重试 {sum(r['attempts'] > 1 for r in rows)}；"
              f"悬空关系（端点不在实体里）{sum(r['dangling'] for r in rows)} 条已丢弃")
        print(f"   实体 {len(ents)} 个（规范化合并后 {len(merged)} 个，其中同名不同类型 {sum(len(t) > 1 for t in merged.values())} 个），关系 {len(rels)} 条")
        print("   出现最多的实体：", sorted(((len([e for e in ents if norm(e['name']) == k]), k) for k in merged), reverse=True)[:12])
        out_path = Path(__file__).parent / "results" / f"kg_extract_{name}_{version}.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"   逐块结果：{out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
