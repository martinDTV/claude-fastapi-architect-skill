import asyncio

from logistics.services import shipments


async def test_fan(session) -> None:
    await asyncio.gather(shipments.get(session, 1), shipments.get(session, 2))          # truth: SA_SESSION_SHARED_CONCURRENTLY
