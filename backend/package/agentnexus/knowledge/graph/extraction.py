"""实体与关系抽取（decisions/0019）。

每个分块调用一次模型，用结构化输出（强制工具调用 + Pydantic）拿到实体和关系。
提示词第 2 版按选型实验的结论收紧：限制每段的实体数量；数值、期限、代码标识符、状态码不单独成为节点；
谓词优先从推荐清单里选。抽取结果按（知识库, 文本哈希, PROMPT_VERSION）缓存，改了提示词就把版本号加一。
"""

import dataclasses
import re
import unicodedata
from typing import Literal, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = 2
MAX_ENTITIES = 8
MAX_RELATIONS = 12

ENTITY_TYPES = ("产品服务", "规则政策", "流程操作", "组织角色", "渠道地点", "技术组件", "概念术语")
EntityType = Literal["产品服务", "规则政策", "流程操作", "组织角色", "渠道地点", "技术组件", "概念术语"]


class Entity(BaseModel):
    name: str = Field(description="实体名称：最常见、最短的说法，一般不超过 12 个字")
    type: EntityType
    description: str = Field(description="一句话说明，不超过 40 个字，只依据原文；数值、期限写在这里")


class Relation(BaseModel):
    source: str = Field(description="起点实体名，必须出现在 entities 里")
    target: str = Field(description="终点实体名，必须出现在 entities 里")
    relation: str = Field(description="关系谓词，2~6 个字，优先从推荐清单里选")
    description: str = Field(description="一句话说明这条关系，不超过 40 个字，只依据原文")


class Extraction(BaseModel):
    """从一段知识库文本中抽取的实体与关系。"""

    entities: list[Entity]
    relations: list[Relation]

    # 超出上限时截断而不是报错：模型偶尔多给几个，丢掉末尾（通常是次要的）比整块重试便宜
    @field_validator("entities")
    @classmethod
    def _cap_entities(cls, v):
        return v[:MAX_ENTITIES]

    @field_validator("relations")
    @classmethod
    def _cap_relations(cls, v):
        return v[:MAX_RELATIONS]


PROMPT = """从下面的知识库片段中抽取实体和实体之间的关系，用于构建知识图谱。
要求：
- 只抽取原文明确提到的内容，不要推测、不要补充常识。
- 实体是值得在图里单独成为节点的东西：产品服务、规则政策、流程操作、组织角色、渠道地点、技术组件、概念术语。每段最多 {max_entities} 个，挑最重要的。
- 下面这些不要作为实体，写进相关实体或关系的描述里：数字、金额、期限、日期、百分比；代码里的函数名、变量名、文件路径、HTTP 状态码；整句话。
- 同一个东西用同一个名字，用最常见、最短的说法（例如写“无理由退货”，不要写“七天无理由退货政策”）。
- 关系有方向：source 对 target 做了什么、有什么约束。谓词优先从这些里选：包含、属于、适用于、不适用于、需要、依赖、替代、优于、导致、通过、提供、限制。两端都必须是你列出的实体。
- 没有值得抽取的内容时，返回空列表。

片段（来自《{title}》）：
{text}"""


# 费用预估（每块）：输入 ≈ 固定开销（提示词 + schema）+ 原文字数，输出 ≈ 基数 + 系数 × 原文字数。
# 系数用第 2 版提示词在两份语料（30 + 69 块）上的实测拟合：决策记录语料预估输入 6.9 万 / 输出 3.7 万 token，
# 实际 6.6 万 / 3.8 万；预估按未命中缓存的价格算，金额偏高约 1/4（有意保守）。构建记录同时保存预估与实际，便于继续校准
EST_INPUT_OVERHEAD = 700
EST_OUTPUT_BASE = 300
EST_OUTPUT_PER_CHAR = 0.8


def estimate_tokens(char_counts: list[int]) -> tuple[int, int]:
    n, chars = len(char_counts), sum(char_counts)
    return n * EST_INPUT_OVERHEAD + chars, int(n * EST_OUTPUT_BASE + EST_OUTPUT_PER_CHAR * chars)


@dataclasses.dataclass(frozen=True)
class ExtractResult:
    payload: dict  # {"entities": [...], "relations": [...]}，悬空关系已丢弃
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


class GraphExtractor(Protocol):
    async def extract(self, title: str, text: str) -> ExtractResult: ...


def entity_key(name: str) -> str:
    """规范化名称（合并的依据）：全角转半角、去掉空白、英文转小写。"""
    return "".join(unicodedata.normalize("NFKC", name).split()).lower()


def _clean(ext: Extraction) -> dict:
    """去掉空名、合并同一段里重复的实体，丢弃两端不在实体里的关系和自环。"""
    entities, seen = [], set()
    for e in ext.entities:
        key = entity_key(e.name)
        if key and key not in seen:
            seen.add(key)
            entities.append({"name": e.name.strip(), "type": e.type, "description": e.description.strip()})
    relations = []
    for r in ext.relations:
        s, t = entity_key(r.source), entity_key(r.target)
        if s in seen and t in seen and s != t and r.relation.strip():
            relations.append({"source": r.source.strip(), "target": r.target.strip(),
                              "relation": r.relation.strip()[:32], "description": r.description.strip()})
    return {"entities": entities, "relations": relations}


class LLMGraphExtractor:
    def __init__(self, model: BaseChatModel):
        self._structured = model.with_structured_output(Extraction, include_raw=True)

    async def extract(self, title: str, text: str) -> ExtractResult:
        prompt = PROMPT.format(title=title, text=text, max_entities=MAX_ENTITIES)
        res = await self._structured.ainvoke(prompt)
        if res["parsed"] is None:  # 格式不对：重试一次（选型实验 99 块里没有发生过）
            res = await self._structured.ainvoke(prompt)
        if res["parsed"] is None:
            raise ValueError(f"extraction output could not be parsed: {res.get('parsing_error')}")
        usage = res["raw"].usage_metadata or {}
        return ExtractResult(
            payload=_clean(res["parsed"]),
            input_tokens=usage.get("input_tokens", 0),
            cached_tokens=(usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
            output_tokens=usage.get("output_tokens", 0),
        )


_PHRASE = re.compile(r"[一-鿿]{2,6}|[A-Za-z][A-Za-z0-9_-]{2,}")


class FakeGraphExtractor:
    """规则版假抽取器：取片段里最先出现的几个词作实体，相邻的连一条“相关”。

    不需要密钥就能演示“构建图谱 → 浏览”的完整流程，测试也不花钱；不追求抽取质量。
    """

    async def extract(self, title: str, text: str) -> ExtractResult:
        names = list(dict.fromkeys(m.group() for m in _PHRASE.finditer(text)))[:5]
        ext = Extraction(
            entities=[Entity(name=n, type="概念术语", description=f"出现在《{title}》中") for n in names],
            relations=[Relation(source=a, target=b, relation="相关", description="在同一段中相邻出现")
                       for a, b in zip(names, names[1:])],
        )
        return ExtractResult(payload=_clean(ext))
