from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentnexus.repositories.users import UserRepository
from agentnexus.services.errors import UserExists
from agentnexus.storage.postgres import User


class UserService:
    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def create(self, name: str) -> User:
        try:
            async with self.sessionmaker() as session, session.begin():
                return await UserRepository(session).create(name)
        except IntegrityError as exc:
            # 唯一约束兜底：并发创建同名用户时，先查后插会有竞态，这里以数据库为准
            raise UserExists(name) from exc
