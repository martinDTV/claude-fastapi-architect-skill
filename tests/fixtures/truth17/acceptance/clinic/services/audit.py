import asyncio
from uuid import UUID

import httpx
import requests
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth import Staff


class AuditLog:
    def __init__(self, db: AsyncSession, http: httpx.AsyncClient) -> None:
        self._db = db
        self._http = http

    async def record(self, staff: Staff, action: str) -> None:
        await self._db.execute(text("insert into audit (staff_id, action) values (:s, :a)"), {"s": staff.id, "a": action})

    async def record_twice(self, staff: Staff) -> None:
        self.record(staff, "a")                                                        # truth: ASYNC_COROUTINE_NOT_AWAITED
        await self.record(staff, "b")

    async def _first(self) -> None:
        await self._db.execute(text("select 1"))

    async def _second(self) -> None:
        await self._db.execute(text("select 2"))

    async def both(self) -> None:
        await asyncio.gather(self._first(), self._second())                            # truth: SA_SESSION_SHARED_CONCURRENTLY

    async def fetch_both(self) -> tuple:
        return await asyncio.gather(self._http.get("/a"), self._http.get("/b"))         # truth-absent: SA_SESSION_SHARED_CONCURRENTLY

    async def guarded(self, staff: Staff) -> None:
        lock = asyncio.Lock()                                                          # truth: ASYNC_LOCK_PER_CALL
        async with lock:
            await self.record(staff, "guarded")


def legacy_sync_lookup(record_id: UUID) -> dict:
    http = requests.Session()
    return http.get(f"https://legacy.invalid/records/{record_id}").json()             # truth: REL_HTTP_CALL_WITHOUT_TIMEOUT
