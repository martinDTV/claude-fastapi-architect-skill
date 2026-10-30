import asyncio
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth import Driver


async def get(session: AsyncSession, shipment_id: UUID) -> dict:
    row = await session.execute(text("select * from shipments where id = :id"), {"id": shipment_id})
    return dict(row.mappings().one())


async def get_for_driver(session: AsyncSession, driver: Driver, shipment_id: UUID) -> dict:
    row = await session.execute(text("select * from shipments where id = :id and depot_id = :d"), {"id": shipment_id, "d": driver.depot_id})
    return dict(row.mappings().one())


async def list_for_depot(session: AsyncSession, depot_id: UUID) -> list:
    rows = await session.execute(text("select id from shipments where depot_id = :d"), {"d": depot_id})
    return list(rows.scalars())


async def reroute(session: AsyncSession, shipment_id: UUID, depot_id: UUID) -> None:
    await session.execute(text("update shipments set depot_id = :d where id = :id"), {"d": depot_id, "id": shipment_id})
    await session.commit()


class Tracker:
    def __init__(self, session: AsyncSession, http: httpx.AsyncClient) -> None:
        self._session = session
        self._http = http

    async def ping(self, shipment_id: UUID) -> None:
        await self._http.post("/track", json={"id": str(shipment_id)})

    async def ping_and_forget(self, shipment_id: UUID) -> None:
        self.ping(shipment_id)                                                         # truth: ASYNC_COROUTINE_NOT_AWAITED
        await self._session.execute(text("update shipments set pinged = true where id = :id"), {"id": shipment_id})

    async def _mark(self, shipment_id: UUID) -> None:
        await self._session.execute(text("update shipments set marked = true where id = :id"), {"id": shipment_id})

    async def _stamp(self, shipment_id: UUID) -> None:
        await self._session.execute(text("update shipments set stamped = now() where id = :id"), {"id": shipment_id})

    async def mark_and_stamp(self, shipment_id: UUID) -> None:
        await asyncio.gather(self._mark(shipment_id), self._stamp(shipment_id))          # truth: SA_SESSION_SHARED_CONCURRENTLY

    async def fan_out(self, ids: list) -> list:
        return await asyncio.gather(*[self._http.get(f"/track/{i}") for i in ids])      # truth-absent: SA_SESSION_SHARED_CONCURRENTLY

    async def exclusive(self, shipment_id: UUID) -> None:
        guard = asyncio.Lock()                                                         # truth: ASYNC_LOCK_PER_CALL
        async with guard:
            await self._mark(shipment_id)


async def quote(shipment_id: UUID) -> dict:
    http = httpx.AsyncClient(base_url="https://carrier.invalid", timeout=10)          # truth: REL_HTTP_CLIENT_PER_CALL
    return (await http.get(f"/quotes/{shipment_id}")).json()


async def quote_managed(shipment_id: UUID) -> dict:
    async with httpx.AsyncClient(base_url="https://carrier.invalid", timeout=10) as http:  # truth-absent: REL_HTTP_CLIENT_PER_CALL
        return (await http.get(f"/quotes/{shipment_id}")).json()
