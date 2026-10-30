"""P1 in a warehouse domain: origins of locals decide the classification; no project edge is invented."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from . import db


class Picker:
    def __init__(self, http: httpx.AsyncClient, session: AsyncSession) -> None:
        self._http = http
        self._session = session

    async def pull(self) -> list:
        resp = await self._http.post("/orders/next", json={})
        payload = resp.json()                                                          # truth-unresolved: external
        return payload.get("lines", [])                                                # truth-unresolved: external

    def split_sku(self, sku: str) -> int:
        family, size = sku.split("-", 1)
        return len(family.strip()) + len(size.strip())                                 # truth-unresolved: builtin

    def per_bin(self, bins: dict) -> list:
        return [code.lower() for code in bins]                                         # truth-unresolved: dynamic

    def totals(self, lines: list) -> int:
        total = 0
        for line in lines:
            total += line.quantity()                                                   # truth-unresolved: dynamic
        return total

    async def with_closure(self) -> None:
        async def flush() -> None:
            self._session.execute("select 1")                                          # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=verified

        await flush()

    async def describe(self, code: str) -> str:
        location = await db.load_location(self._session, code)
        return location.describe()                                                     # truth-edge: wms.db.Location.describe

    def label(self, code: str) -> str:
        text = db.normalise(code)
        return text.lower()                                                            # truth-unresolved: builtin

    async def route_like(self, code: str) -> str:
        location = await db.load_location(self._session, code)
        return location.describe()                                                     # truth: ASYNC_BLOCKING_CALL_INDIRECT
