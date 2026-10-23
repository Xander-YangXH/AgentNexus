"""登录与会话（decisions/0023）：argon2id 密码哈希 + 服务端会话（HttpOnly Cookie 里只放随机令牌）。

- 哈希在线程池里算：实验中 8 个并发登录时，在事件循环里算会让别的请求等 254ms，线程池里只等 3ms；
- 用户不存在时也做一次同样耗时的校验：响应时间不泄露“这个用户名是否存在”；
- 会话表只存令牌的 SHA-256；退出 = 删除会话，禁用用户 = 会话立刻失效（校验时检查 disabled_at）。
"""

import asyncio
import dataclasses
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.security.principal import Principal
from agentnexus.services.errors import InvalidCredentials, UserExists, UserNotFound
from agentnexus.storage.postgres import Department, Role, User, UserSession

# OWASP 推荐参数（m=19MiB, t=2, p=1），实测单次校验约 15ms
_hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")
MIN_PASSWORD_LENGTH = 8


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    def check():
        try:
            return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(check)


@dataclasses.dataclass(frozen=True)
class LoginResult:
    principal: Principal
    token: str
    expires_at: datetime


def _principal(user: User) -> Principal:
    return Principal(id=user.id, name=user.name, role=user.role, department_id=user.department_id)


class AuthService:
    def __init__(self, sessionmaker: async_sessionmaker, *, session_ttl: timedelta = timedelta(days=7)):
        self.sessionmaker = sessionmaker
        self.session_ttl = session_ttl

    async def login(self, name: str, password: str) -> LoginResult:
        async with self.sessionmaker() as session:
            user = await session.scalar(select(User).where(User.name == name))
        ok = await verify_password(user.password_hash if user else None, password)
        if not ok or user is None or user.disabled_at is not None:
            raise InvalidCredentials()  # 不区分“用户不存在 / 密码错 / 已禁用”
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + self.session_ttl
        async with self.sessionmaker() as session, session.begin():
            session.add(UserSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
        return LoginResult(_principal(user), token, expires)

    async def resolve(self, token: str | None) -> Principal | None:
        if not token:
            return None
        stmt = (select(User).join(UserSession, UserSession.user_id == User.id)
                .where(UserSession.token_hash == token_hash(token), UserSession.expires_at > func.now(),
                       User.disabled_at.is_(None)))
        async with self.sessionmaker() as session:
            user = await session.scalar(stmt)
        return _principal(user) if user else None

    async def logout(self, token: str | None) -> None:
        if token:
            async with self.sessionmaker() as session, session.begin():
                await session.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))

    async def purge_expired(self) -> int:
        async with self.sessionmaker() as session, session.begin():
            return (await session.execute(delete(UserSession).where(UserSession.expires_at <= func.now()))).rowcount

    # ---------- 管理员操作（接口层检查角色） ----------

    async def create_user(self, name: str, password: str, *, role: str = Role.MEMBER,
                          department_id: uuid.UUID | None = None) -> User:
        password_hash = await hash_password(password)
        try:
            async with self.sessionmaker() as session, session.begin():
                user = User(name=name, password_hash=password_hash, role=role, department_id=department_id)
                session.add(user)
                await session.flush()
                return user
        except IntegrityError as exc:
            raise UserExists(name) from exc

    async def update_user(self, user_id: uuid.UUID, *, role=None, department_id=dataclasses.MISSING,
                          disabled: bool | None = None, password: str | None = None) -> User:
        password_hash = await hash_password(password) if password else None
        async with self.sessionmaker() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise UserNotFound(str(user_id))
            if role is not None:
                user.role = role
            if department_id is not dataclasses.MISSING:
                user.department_id = department_id
            if disabled is not None:
                user.disabled_at = func.now() if disabled else None
            if password_hash:
                user.password_hash = password_hash
            if disabled or password_hash:
                # 禁用、重置密码：已有会话全部作废
                await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await session.flush()
            await session.refresh(user)
            return user

    async def list_users(self) -> list[User]:
        async with self.sessionmaker() as session:
            return list((await session.scalars(select(User).order_by(User.created_at))).all())

    async def create_department(self, name: str) -> Department:
        try:
            async with self.sessionmaker() as session, session.begin():
                dept = Department(name=name)
                session.add(dept)
                await session.flush()
                return dept
        except IntegrityError as exc:
            raise UserExists(name) from exc  # 名称重复，统一按 409 处理

    async def list_departments(self) -> list[Department]:
        async with self.sessionmaker() as session:
            return list((await session.scalars(select(Department).order_by(Department.name))).all())
