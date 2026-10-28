import asyncio
import time

import httpx
import requests
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Account, Order


async def commit_per_row(session: AsyncSession, orders: list[Order]) -> None:
    for order in orders:
        order.status = "shipped"
        await session.commit()                                      # commit inside the loop


async def commit_once(session: AsyncSession, orders: list[Order]) -> None:
    for order in orders:
        order.status = "shipped"
    await session.commit()                                          # single commit: fine


async def http_inside_transaction(session: AsyncSession, client: httpx.AsyncClient) -> None:
    async with session.begin():
        await client.post("https://partner.invalid/notify")         # network call holding the transaction
        requests.get("https://partner.invalid/ping")                # sync module helper too
        time.sleep(1)                                               # sleeping inside the transaction


async def http_outside_transaction(session: AsyncSession, client: httpx.AsyncClient) -> None:
    async with session.begin():
        session.add(Order())
    await client.post("https://partner.invalid/notify")             # after the transaction: fine


async def lost_update(session: AsyncSession, account_id: int, amount: int) -> None:
    account = await session.get(Account, account_id)
    account.balance += amount                                       # read-modify-write without lock/version
    await session.commit()


async def locked_update(session: AsyncSession, account_id: int, amount: int) -> None:
    result = await session.execute(select(Account).where(Account.id == account_id).with_for_update())
    account = result.scalar_one()
    account.balance += amount                                       # protected by FOR UPDATE
    await session.commit()


async def atomic_update(session: AsyncSession, account_id: int, amount: int) -> None:
    await session.execute(update(Account).where(Account.id == account_id).values(balance=Account.balance + amount))
    await session.commit()


async def swallowed_commit_failure(session: AsyncSession) -> None:
    try:
        session.add(Order())
        await session.commit()
    except Exception:
        pass                                                        # neither rollback nor re-raise


async def rollback_on_failure(session: AsyncSession) -> None:
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def bounded_by_context_manager() -> None:
    try:
        async with SessionLocal() as session:
            session.add(Order())
            await session.commit()                                  # session closed by the context manager
    except Exception:
        return
