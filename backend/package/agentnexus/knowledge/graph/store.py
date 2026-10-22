"""Neo4j 图存储（decisions/0019）：派生数据，只存图查询需要的节点与边，id 与 PostgreSQL 一致。

- 节点 (:Entity {id, kb, name, type, mentions})，边 [:REL {id, predicate, weight}]，方向与抽取时一致；
- 每次构建整体替换一个知识库的子图（先删后写），不做增量合并：图的事实来源在 PostgreSQL，整体替换最简单也最不容易出错；
- 描述、出处等详情不放这里，查 PostgreSQL。
"""

import uuid
from collections.abc import Sequence
from typing import Protocol

from neo4j import AsyncDriver

# 写操作都显式 consume()：保证语句执行完、错误当场抛出，而不是拖到会话关闭时才冒出来

BATCH = 2000


class EntityLike(Protocol):
    """合并结果（MergedEntity）或数据库记录（GraphEntity）都满足：构建时写前者，重建命令写后者。"""

    id: uuid.UUID
    name: str
    type: str
    mention_count: int


class RelationLike(Protocol):
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    predicate: str
    mention_count: int


def _node(n) -> dict:
    return {"id": n["id"], "name": n["name"], "type": n["type"], "mentions": n["mentions"]}


def _edge(r, source_id: str, target_id: str) -> dict:
    return {"id": r["id"], "source": source_id, "target": target_id, "predicate": r["predicate"], "weight": r["weight"]}


class Neo4jGraphStore:
    def __init__(self, driver: AsyncDriver):
        self.driver = driver

    async def ensure_schema(self) -> None:
        async with self.driver.session() as s:
            for stmt in ("CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
                         "CREATE INDEX entity_kb IF NOT EXISTS FOR (e:Entity) ON (e.kb)",
                         "CREATE TEXT INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)"):
                await (await s.run(stmt)).consume()

    async def replace_kb(
        self, kb_id: uuid.UUID, entities: Sequence[EntityLike], relations: Sequence[RelationLike]
    ) -> None:
        kb = str(kb_id)
        async with self.driver.session() as s:
            await self._delete(s, kb)
            for i in range(0, len(entities), BATCH):
                rows = [{"id": str(e.id), "name": e.name, "type": e.type, "mentions": e.mention_count}
                        for e in entities[i:i + BATCH]]
                await (await s.run(
                    "UNWIND $rows AS r CREATE (:Entity {id: r.id, kb: $kb, name: r.name, type: r.type, mentions: r.mentions})",
                    rows=rows, kb=kb)).consume()
            for i in range(0, len(relations), BATCH):
                rows = [{"id": str(r.id), "s": str(r.source_id), "t": str(r.target_id), "p": r.predicate, "w": r.mention_count}
                        for r in relations[i:i + BATCH]]
                await (await s.run("UNWIND $rows AS r MATCH (a:Entity {id: r.s}), (b:Entity {id: r.t}) "
                                   "CREATE (a)-[:REL {id: r.id, predicate: r.p, weight: r.w}]->(b)", rows=rows)).consume()

    async def delete_kb(self, kb_id: uuid.UUID) -> None:
        async with self.driver.session() as s:
            await self._delete(s, str(kb_id))

    @staticmethod
    async def _delete(session, kb: str) -> None:
        # 分批删除：一次性 DETACH DELETE 一个大子图会占用大量事务内存
        await (await session.run("MATCH (e:Entity {kb: $kb}) CALL (e) { DETACH DELETE e } IN TRANSACTIONS OF 5000 ROWS",
                                 kb=kb)).consume()

    async def count(self, kb_id: uuid.UUID) -> tuple[int, int]:
        async with self.driver.session() as s:
            rec = await (await s.run(
                "MATCH (e:Entity {kb: $kb}) OPTIONAL MATCH (e)-[r:REL]->() RETURN count(DISTINCT e) AS n, count(r) AS m",
                kb=str(kb_id))).single()
            return rec["n"], rec["m"]

    # ---------- 查询（调用方负责校验知识库归属） ----------

    async def search(self, kb_id: uuid.UUID, q: str, limit: int = 20) -> list[dict]:
        async with self.driver.session() as s:
            res = await s.run(
                "MATCH (e:Entity) WHERE e.kb = $kb AND toLower(e.name) CONTAINS toLower($q) "
                "RETURN e ORDER BY e.mentions DESC, e.name LIMIT $limit", kb=str(kb_id), q=q, limit=limit)
            return [_node(r["e"]) async for r in res]

    async def overview(self, kb_id: uuid.UUID, limit: int = 60) -> dict:
        """初始视图：被提及最多的实体，以及它们之间的边。"""
        async with self.driver.session() as s:
            res = await s.run("MATCH (e:Entity {kb: $kb}) RETURN e ORDER BY e.mentions DESC, e.name LIMIT $limit",
                              kb=str(kb_id), limit=limit)
            nodes = [_node(r["e"]) async for r in res]
            ids = [n["id"] for n in nodes]
            res = await s.run("MATCH (a:Entity)-[r:REL]->(b:Entity) WHERE a.id IN $ids AND b.id IN $ids RETURN a.id AS s, b.id AS t, r",
                              ids=ids)
            edges = [_edge(r["r"], r["s"], r["t"]) async for r in res]
        return {"nodes": nodes, "edges": edges}

    async def neighbors(self, entity_id: uuid.UUID, limit: int = 40) -> dict:
        """一跳展开：按邻居的提及次数取前 limit 个（枢纽节点可能有上千个邻居，全部画出来没法看）。"""
        async with self.driver.session() as s:
            res = await s.run(
                "MATCH (e:Entity {id: $id})-[r:REL]-(n:Entity) "
                "WITH e, r, n ORDER BY n.mentions DESC, r.weight DESC LIMIT $limit "
                "RETURN e, n, r, startNode(r) = e AS outgoing", id=str(entity_id), limit=limit)
            nodes, edges = {}, []
            async for row in res:
                e, n = _node(row["e"]), _node(row["n"])
                nodes[e["id"]], nodes[n["id"]] = e, n
                src, dst = (e["id"], n["id"]) if row["outgoing"] else (n["id"], e["id"])
                edges.append(_edge(row["r"], src, dst))
            if not nodes:  # 孤立节点：也要把它自己返回
                rec = await (await s.run("MATCH (e:Entity {id: $id}) RETURN e", id=str(entity_id))).single()
                if rec:
                    nodes[rec["e"]["id"]] = _node(rec["e"])
        return {"nodes": list(nodes.values()), "edges": edges}

    async def shortest_path(self, kb_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID, max_hops: int = 4) -> dict | None:
        """两个实体之间的最短关联（不看方向）。max_hops 必须是常量写进查询：Cypher 的路径长度不能用参数。"""
        hops = max(1, min(int(max_hops), 6))
        async with self.driver.session() as s:
            rec = await (await s.run(
                "MATCH (a:Entity {id: $a, kb: $kb}), (b:Entity {id: $b, kb: $kb}) "
                f"MATCH p = shortestPath((a)-[:REL*..{hops}]-(b)) RETURN nodes(p) AS ns, relationships(p) AS rs",
                a=str(a), b=str(b), kb=str(kb_id))).single()
        if rec is None:
            return None
        return {"nodes": [_node(n) for n in rec["ns"]],
                "edges": [_edge(r, r.start_node["id"], r.end_node["id"]) for r in rec["rs"]]}
