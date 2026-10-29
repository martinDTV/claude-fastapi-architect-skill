from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shop.domain.model import Order


class SqlOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, order: Order) -> None:
        self.session.add(order)
        await self.session.flush()

    async def get(self, order_id: int):
        return await self.session.get(Order, order_id)


class SqlUnitOfWork:
    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory

    async def __aenter__(self):
        self.session = self.factory()
        self.repo = SqlOrderRepository(self.session)
        return self

    async def __aexit__(self, *exc) -> None:
        if exc[0] is not None:
            await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()
