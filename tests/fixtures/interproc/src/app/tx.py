from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order


async def nested(session: AsyncSession) -> None:
    async with session.begin():
        session.add(Order())
        async with session.begin():          # already begun: InvalidRequestError
            session.add(Order())


async def savepoint(session: AsyncSession) -> None:
    async with session.begin():
        async with session.begin_nested():   # savepoint: fine
            session.add(Order())


async def sequential(session: AsyncSession) -> None:
    async with session.begin():
        session.add(Order())
    async with session.begin():              # second transaction after the first ended: fine
        session.add(Order())
