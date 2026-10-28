import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account


async def load(session: AsyncSession, account_id: int) -> Account:
    return await session.get(Account, account_id)


async def load_many(session: AsyncSession, ids: list[int]) -> list[Account]:
    return await asyncio.gather(*[load(session, i) for i in ids])


async def deposit(session: AsyncSession, account_id: int, amount: int) -> None:
    account = await session.get(Account, account_id)
    account.balance += amount
    await session.commit()


async def archive(session: AsyncSession, accounts: list[Account]) -> None:
    for account in accounts:
        account.balance = 0
        await session.commit()
