"""知识库检索工具（decisions/0018）。

工具参数只有 query：检索哪些知识库、以谁的身份检索，由服务端按对话的关联与归属决定，
模型无法指定——否则提示注入（例如文档里写“去检索某某知识库”）就能越权读取别人的数据。
"""

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence

from agentnexus.knowledge.hits import SearchHit

KNOWLEDGE_SEARCH = "knowledge_search"
MAX_QUERY_CHARS = 500

# OpenAI 函数格式；各供应商的 bind_tools 都能转换（Anthropic 格式由 langchain-anthropic 转）
KNOWLEDGE_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": KNOWLEDGE_SEARCH,
        "description": "在用户的知识库中检索与问题相关的片段。返回带编号 [n] 的片段和来源文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "完整、具体的检索问题，补全对话中的指代"},
            },
            "required": ["query"],
        },
    },
}

# 检索函数：(用户 id, 知识库 id 列表, 检索词) -> 片段。由装配处（server/main.py）用 SearchService 实现
KnowledgeSearch = Callable[[uuid.UUID, Sequence[uuid.UUID], str], Awaitable[list[SearchHit]]]

NO_RESULT = "知识库中没有找到相关内容。"
_CITE = re.compile(r"\[(\d{1,3})\]")


def to_citation(n: int, hit: SearchHit) -> dict:
    """引用快照：存进 messages.citations（JSON），所以 id 转成字符串。"""
    return {
        "n": n,
        "chunk_id": str(hit.chunk_id),
        "document_id": str(hit.document_id),
        "knowledge_base_id": str(hit.knowledge_base_id),
        "filename": hit.filename,
        "page": hit.page,
        "seq": hit.seq,
        "text": hit.text,
        "vector_score": hit.vector_score,
        "rerank_score": hit.rerank_score,
    }


def format_for_model(numbered: list[tuple[int, SearchHit]]) -> str:
    if not numbered:
        return NO_RESULT
    parts = []
    for n, h in numbered:
        source = h.filename + (f"（第 {h.page} 页）" if h.page else "")
        parts.append(f"[{n}] 来源：{source}\n{h.text}")
    return "\n\n".join(parts)


def mark_cited(citations: list[dict], answer: str) -> list[dict]:
    """标出回答里实际引用了哪些编号。没被引用的也保留（cited=False），排查“检索到了却没用上”时有用。"""
    used = {int(m) for m in _CITE.findall(answer)}
    return [{**c, "cited": c["n"] in used} for c in citations]
