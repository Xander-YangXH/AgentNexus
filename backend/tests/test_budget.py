"""每个用户最近 24 小时的模型费用上限（阶段 7.9，decisions/0032）。"""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import update

from agentnexus.repositories.conversations import ConversationRepository
from agentnexus.services.errors import QuotaExceeded
from agentnexus.services.runs import Budget, RunService
from agentnexus.services.usage import usage_insert
from agentnexus.storage.postgres import AgentRun, Role, UsageEvent, UsageKind, User
from server.deps import get_dispatcher, get_sessionmaker
from server.main import create_app
from tests.fakes import install_test_auth

# 每百万 token：输入 $1、缓存命中 $0.02、输出 $2；上限 $0.01
BUDGET = Budget(0.01, price_input=1.0, price_cached=0.02, price_output=2.0)


async def _user(sessionmaker, role=Role.MEMBER):
    async with sessionmaker() as s, s.begin():
        u = User(name=f"budget-{uuid.uuid4().hex[:8]}", role=role)
        s.add(u)
    async with sessionmaker() as s, s.begin():
        conv = await ConversationRepository(s).create(u.id)
    return u, conv


async def _spend(sessionmaker, user, *, input_tokens=0, cached=0, output=0, hours_ago=0):
    async with sessionmaker() as s, s.begin():
        await s.execute(usage_insert(user.id, UsageKind.LLM, "chat", input_tokens=input_tokens,
                                     cached_tokens=cached, output_tokens=output))
        if hours_ago:
            await s.execute(update(UsageEvent).where(UsageEvent.user_id == user.id)
                            .values(created_at=UsageEvent.created_at - timedelta(hours=hours_ago)))


async def test_new_questions_are_refused_once_the_daily_budget_is_used(sessionmaker):
    user, conv = await _user(sessionmaker)
    svc = RunService(sessionmaker, budget=BUDGET)
    await _spend(sessionmaker, user, input_tokens=4000, output=2000)  # $0.004 + $0.004 = $0.008：还没到
    run = await svc.submit(user.id, conv.id, "第一个问题")
    async with sessionmaker() as s, s.begin():
        await s.execute(update(AgentRun).where(AgentRun.id == run.id).values(status="failed"))
    await _spend(sessionmaker, user, output=1000)  # 再 $0.002：到 $0.010
    with pytest.raises(QuotaExceeded) as exc:
        await svc.submit(user.id, conv.id, "第二个问题")
    assert exc.value.used_usd == pytest.approx(0.01) and exc.value.limit_usd == 0.01
    with pytest.raises(QuotaExceeded):
        await svc.retry(user.id, run.id)  # 重试同样要花钱，同样检查


async def test_cached_input_is_cheap_and_old_usage_does_not_count(sessionmaker):
    user, conv = await _user(sessionmaker)
    svc = RunService(sessionmaker, budget=BUDGET)
    # 按 token 数是 40 万（按全价算要 $0.4），按费用只有 1000×$1/M + 40 万×$0.02/M = $0.009：不拦
    await _spend(sessionmaker, user, input_tokens=401_000, cached=400_000, output=0)
    await svc.submit(user.id, conv.id, "多轮长对话")
    other, conv2 = await _user(sessionmaker)
    await _spend(sessionmaker, other, output=100_000, hours_ago=25)  # $0.2，但在 24 小时之前
    await svc.submit(other.id, conv2.id, "昨天花得多，今天还能问")


async def test_admins_and_a_zero_budget_are_not_limited(sessionmaker):
    admin, conv = await _user(sessionmaker, role=Role.ADMIN)
    await _spend(sessionmaker, admin, output=1_000_000)
    await RunService(sessionmaker, budget=BUDGET).submit(admin.id, conv.id, "管理员不限")
    user, conv2 = await _user(sessionmaker)
    await _spend(sessionmaker, user, output=1_000_000)
    await RunService(sessionmaker, budget=Budget(0, 1, 1, 1)).submit(user.id, conv2.id, "上限为 0 表示不限")


async def test_the_api_answers_429_with_the_numbers(sessionmaker):
    user, conv = await _user(sessionmaker)
    await _spend(sessionmaker, user, output=10_000)  # $0.02
    app = create_app(with_lifespan=False)
    install_test_auth(app)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_dispatcher] = lambda: type("D", (), {"enqueue": lambda self, _: True})()
    app.state.run_budget = BUDGET
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/conversations/{conv.id}/messages", json={"content": "还能问吗"},
                         headers={"X-User-Id": str(user.id)})
    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "quota_exceeded" and body["details"] == {"used_usd": 0.02, "limit_usd": 0.01}
