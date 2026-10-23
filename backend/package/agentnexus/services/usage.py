"""用量（阶段 5，decisions/0023）：采集明细 + 查询时聚合。

采集：
- 请求：提交 run 时，与用户消息同一事务写一行（kind=run）；
- 模型调用：LangChain 回调，每次调用结束立刻写一行（kind=llm）。实验：图配置里挂一个回调，节点内部的调用
  （结构化输出、bind_tools、Send 并行的子 Agent、流式）都能捕获；而且与图是否提交无关——
  4.3 把用量记在图状态里，崩溃那次执行中“在途”的调用会丢，这里不会；
- 检索：每次检索一行（kind=search）；图谱抽取：每次抽取一行（source=graph）。
写用量失败只记日志，不影响业务。

聚合：Dashboard 直接在明细上按天 GROUP BY（实验：百万行时个人 / 部门 5~21ms）。数据量大了再加汇总表。
可见范围：本人；部门负责人看本部门；管理员看全部或指定部门。数据库的 RLS 策略也按同样的规则限制。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from langchain_core.callbacks import AsyncCallbackHandler
from sqlalchemy import Date, case, cast, func, insert, literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.security.principal import Principal
from agentnexus.storage.postgres import Role, UsageEvent, UsageKind, User

log = logging.getLogger(__name__)


def usage_insert(user_id: uuid.UUID, kind: str, source: str, *, run_id=None, model=None, input_tokens=0,
                 cached_tokens=0, output_tokens=0):
    """一条明细的 INSERT。部门取用户“当时”所在的部门（子查询）：用户换部门后，历史用量仍算在原部门。"""
    dept = select(User.department_id).where(User.id == user_id).scalar_subquery()
    return insert(UsageEvent).values(
        user_id=user_id, department_id=dept, kind=kind, source=source, run_id=run_id, model=model,
        input_tokens=input_tokens, cached_tokens=cached_tokens, output_tokens=output_tokens)


class UsageRecorder:
    def __init__(self, sessionmaker: async_sessionmaker, *, model: str | None = None):
        self.sessionmaker = sessionmaker
        self.model = model

    async def record(self, user_id: uuid.UUID | None, kind: str, source: str, **kw) -> None:
        if user_id is None:
            return
        try:
            async with self.sessionmaker() as session, session.begin():
                await session.execute(usage_insert(user_id, kind, source, **kw))
        except Exception:
            log.warning("recording usage failed", exc_info=True)

    def callback(self, user_id: uuid.UUID | None, source: str, run_id: uuid.UUID | None = None) -> "UsageCallback":
        return UsageCallback(self, user_id, source, run_id)


class UsageCallback(AsyncCallbackHandler):
    """挂在图的配置上（config["callbacks"]），每次模型调用结束写一行明细。一个 run 一个实例。"""

    def __init__(self, recorder: UsageRecorder, user_id, source: str, run_id):
        self.recorder, self.user_id, self.source, self.run_id = recorder, user_id, source, run_id

    async def on_llm_end(self, response, **kwargs) -> None:
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
        except (IndexError, AttributeError):
            usage = {}
        await self.recorder.record(
            self.user_id, UsageKind.LLM, self.source, run_id=self.run_id, model=self.recorder.model,
            input_tokens=usage.get("input_tokens", 0) or 0,
            cached_tokens=(usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
        )


class UsageService:
    def __init__(self, sessionmaker: async_sessionmaker, *, price_input: float, price_cached: float, price_output: float):
        self.sessionmaker = sessionmaker
        self.prices = (price_input, price_cached, price_output)

    def _cost(self, input_tokens: int, cached: int, output: int) -> float:
        p_in, p_cached, p_out = self.prices
        return round(((input_tokens - cached) * p_in + cached * p_cached + output * p_out) / 1_000_000, 6)

    def scope_filter(self, principal: Principal, scope: str, department_id: uuid.UUID | None):
        """按角色决定能看什么（与 RLS 策略一致；这里显式写出来，越权时返回 403 而不是“空数据”）。"""
        if scope == "me":
            return UsageEvent.user_id == principal.id
        if scope == "department":
            if principal.role == Role.ADMIN and department_id is not None:
                return UsageEvent.department_id == department_id
            if principal.role in (Role.ADMIN, Role.MANAGER) and principal.department_id is not None and department_id in (None, principal.department_id):
                return UsageEvent.department_id == principal.department_id
            raise PermissionError("department usage requires manager or admin")
        if scope == "all" and principal.role == Role.ADMIN:
            return literal(True)
        raise PermissionError(f"scope {scope!r} not allowed")

    async def summary(self, principal: Principal, *, days: int = 30, scope: str = "me",
                      department_id: uuid.UUID | None = None, now: datetime | None = None) -> dict:
        where = self.scope_filter(principal, scope, department_id)
        since = (now or datetime.now(UTC)) - timedelta(days=days)
        day = cast(func.date_trunc("day", UsageEvent.created_at), Date).label("day")
        is_llm = UsageEvent.kind == UsageKind.LLM
        metrics = [
            func.count().filter(UsageEvent.kind == UsageKind.RUN).label("runs"),
            func.count().filter(is_llm).label("llm_calls"),
            func.count().filter(UsageEvent.kind == UsageKind.SEARCH).label("searches"),
            func.coalesce(func.sum(case((is_llm, UsageEvent.input_tokens), else_=0)), 0).label("input_tokens"),
            func.coalesce(func.sum(case((is_llm, UsageEvent.cached_tokens), else_=0)), 0).label("cached_tokens"),
            func.coalesce(func.sum(case((is_llm, UsageEvent.output_tokens), else_=0)), 0).label("output_tokens"),
        ]
        base = select(*metrics).where(where, UsageEvent.created_at >= since)
        async with self.sessionmaker() as session:
            series = (await session.execute(base.add_columns(day).group_by(day).order_by(day))).mappings().all()
            totals = (await session.execute(base)).mappings().one()
            by_user = []
            if scope != "me":
                stmt = (base.add_columns(User.id.label("user_id"), User.name.label("name"))
                        .join(User, User.id == UsageEvent.user_id).group_by(User.id, User.name)
                        .order_by(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens).desc()).limit(20))
                by_user = (await session.execute(stmt)).mappings().all()
        row = lambda r: {**dict(r), "cost_usd": self._cost(r["input_tokens"], r["cached_tokens"], r["output_tokens"])}
        return {
            "scope": scope, "days": days,
            "series": [{**row(r), "day": r["day"].isoformat()} for r in series],
            "totals": row(totals),
            "by_user": [{**row(r), "user_id": str(r["user_id"])} for r in by_user],
        }
