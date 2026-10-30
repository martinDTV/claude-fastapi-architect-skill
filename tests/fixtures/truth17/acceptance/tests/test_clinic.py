import asyncio

from clinic.services import records


async def test_shared(db) -> None:
    await asyncio.gather(records.get(db, 1), records.get(db, 2))                       # truth-absent: SA_SESSION_SHARED_CONCURRENTLY
