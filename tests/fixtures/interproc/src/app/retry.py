import asyncio

import httpx
import tenacity
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order


@tenacity.retry(stop=tenacity.stop_after_attempt(3))
async def charge(client: httpx.AsyncClient, amount: int) -> None:
    await client.post("https://pay.invalid/charge", json={"amount": amount})      # retried write, no key


@tenacity.retry(stop=tenacity.stop_after_attempt(3))
async def charge_idempotent(client: httpx.AsyncClient, amount: int, idempotency_key: str) -> None:
    await client.post("https://pay.invalid/charge", json={"amount": amount}, headers={"Idempotency-Key": idempotency_key})


@tenacity.retry(stop=tenacity.stop_after_attempt(3))
async def read_balance(client: httpx.AsyncClient) -> dict:
    return (await client.get("https://pay.invalid/balance")).json()                # read: fine


async def manual(session: AsyncSession) -> None:
    for attempt in range(3):
        try:
            session.add(Order())
            await session.commit()                                                 # hand-written retry around a write
            break
        except Exception:
            await asyncio.sleep(0.1)


async def poll(session: AsyncSession, stop) -> None:
    while not stop.is_set():                                                       # poll loop, not a retry
        try:
            session.add(Order())
            await session.commit()
        except Exception:
            await asyncio.sleep(1)
