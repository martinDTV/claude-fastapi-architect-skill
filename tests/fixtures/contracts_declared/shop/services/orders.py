from sqlalchemy.ext.asyncio import AsyncSession


async def place(session: AsyncSession, order_id: int) -> None:
    session.add(object())
    await session.commit()
