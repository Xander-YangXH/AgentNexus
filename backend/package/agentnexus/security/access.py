"""知识库的可见性（decisions/0023）：服务层的第一道防线（第二道是数据库的 RLS 策略，见迁移 0011）。

- 读：所有者，或者知识库是“部门共享”且与当前用户同部门；
- 写（上传、重新处理、构建图谱、改可见性）：只有所有者。看得见但不是所有者 → 403（只读），看不见 → 404（不泄露是否存在）。
对话、运行记录、产物始终只属于自己，它们的检查仍在各自的服务里（user_id 相等）。
"""

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.services.errors import KnowledgeBaseNotFound, KnowledgeBaseReadOnly
from agentnexus.storage.postgres import KnowledgeBase, User, Visibility


async def department_of(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(User.department_id).where(User.id == user_id))


def visible_clause(user_id: uuid.UUID, department_id: uuid.UUID | None):
    shared = and_(KnowledgeBase.visibility == Visibility.DEPARTMENT, KnowledgeBase.department_id == department_id)
    return or_(KnowledgeBase.user_id == user_id, shared) if department_id else KnowledgeBase.user_id == user_id


def can_read(kb: KnowledgeBase, user_id: uuid.UUID, department_id: uuid.UUID | None) -> bool:
    return kb.user_id == user_id or (
        kb.visibility == Visibility.DEPARTMENT and department_id is not None and kb.department_id == department_id)


async def readable_kb(session: AsyncSession, user_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or not can_read(kb, user_id, await department_of(session, user_id)):
        raise KnowledgeBaseNotFound(str(kb_id))
    return kb


async def writable_kb(session: AsyncSession, user_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = await readable_kb(session, user_id, kb_id)
    if kb.user_id != user_id:
        raise KnowledgeBaseReadOnly(str(kb_id))
    return kb


async def visible_kb_ids(session: AsyncSession, user_id: uuid.UUID, kb_ids) -> list[uuid.UUID]:
    """给定的知识库里，当前用户看得见的那些（保持原顺序）。"""
    kb_ids = list(kb_ids)
    if not kb_ids:
        return []
    dept = await department_of(session, user_id)
    rows = set(await session.scalars(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids), visible_clause(user_id, dept))))
    return [k for k in kb_ids if k in rows]
