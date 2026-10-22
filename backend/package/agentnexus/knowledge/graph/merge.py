"""把各分块的抽取结果合并成一张图（纯函数，便于测试）。

- 实体按规范化名称合并：显示名称取出现最多的写法，类型多数投票，描述取最长的一条（信息最多），提及次数 = 出现在几个分块里；
- 关系按（起点, 终点, 谓词）合并，权重 = 出现在几个分块里；
- 同时产出溯源：每个实体 / 关系出自哪些分块。
"""

import dataclasses
import uuid
from collections import Counter, defaultdict

from agentnexus.knowledge.graph.extraction import entity_key


@dataclasses.dataclass
class MergedEntity:
    id: uuid.UUID
    key: str
    name: str
    type: str
    description: str
    chunk_ids: list[uuid.UUID]

    @property
    def mention_count(self) -> int:  # 与 GraphEntity 同名：Neo4j 存储对两者一视同仁（重建命令直接传数据库记录）
        return len(self.chunk_ids)


@dataclasses.dataclass
class MergedRelation:
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    predicate: str
    description: str
    chunk_ids: list[uuid.UUID]

    @property
    def mention_count(self) -> int:
        return len(self.chunk_ids)


def merge(extractions: list[tuple[uuid.UUID, dict]]) -> tuple[list[MergedEntity], list[MergedRelation]]:
    """extractions：[(分块 id, 抽取结果)]，同一份抽取结果可以对应多个分块（内容相同的分块只抽一次）。"""
    names: dict[str, Counter] = defaultdict(Counter)
    types: dict[str, Counter] = defaultdict(Counter)
    descs: dict[str, list[str]] = defaultdict(list)
    ent_chunks: dict[str, list[uuid.UUID]] = defaultdict(list)
    rel_desc: dict[tuple, list[str]] = defaultdict(list)
    rel_chunks: dict[tuple, list[uuid.UUID]] = defaultdict(list)

    for chunk_id, payload in extractions:
        for e in payload.get("entities", []):
            k = entity_key(e["name"])
            names[k][e["name"]] += 1
            types[k][e["type"]] += 1
            descs[k].append(e["description"])
            if chunk_id not in ent_chunks[k]:
                ent_chunks[k].append(chunk_id)
        for r in payload.get("relations", []):
            edge = (entity_key(r["source"]), entity_key(r["target"]), r["relation"])
            if edge[0] not in names or edge[1] not in names:
                continue
            rel_desc[edge].append(r["description"])
            if chunk_id not in rel_chunks[edge]:
                rel_chunks[edge].append(chunk_id)

    entities = {
        k: MergedEntity(
            id=uuid.uuid4(), key=k[:200], name=names[k].most_common(1)[0][0][:200], type=types[k].most_common(1)[0][0],
            description=max(descs[k], key=len)[:500], chunk_ids=ent_chunks[k],
        )
        for k in names
    }
    relations = [
        MergedRelation(
            id=uuid.uuid4(), source_id=entities[s].id, target_id=entities[t].id, predicate=p,
            description=max(rel_desc[(s, t, p)], key=len)[:500], chunk_ids=rel_chunks[(s, t, p)],
        )
        for (s, t, p) in rel_desc
    ]
    return list(entities.values()), relations
