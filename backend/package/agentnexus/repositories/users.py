import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agentnexus.storage.postgres import User


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, name: str) -> User:
        user = User(name=name)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)
