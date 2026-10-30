import json

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import load_contact, render


class Syncer:
    def __init__(self, http: httpx.AsyncClient, session: AsyncSession) -> None:
        self._http = http
        self._session = session

    async def pull(self) -> list:
        reply = await self._http.get("/contacts")
        body = reply.json()                                                            # truth-unresolved: external
        return body.get("items", [])                                                   # truth-unresolved: external

    async def push(self, contacts: dict) -> None:
        for email, score in contacts.items():
            await self._http.post("/score", json={"email": email.lower(), "s": score})  # truth-unresolved: dynamic

    def helper_in_closure(self) -> int:
        def count() -> int:
            self._session.execute(text("select 1"))                                    # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=verified
            return 1

        return count()

    async def describe(self, email: str) -> str:
        contact = await load_contact(self._session, email)
        return contact.domain()                                                        # truth-edge: crm.db.Contact.domain

    async def rendered(self, email: str) -> str:
        text_value = await render(self._session, await load_contact(self._session, email))
        return text_value.upper()                                                      # truth-unresolved: builtin


def decode(raw: bytes) -> dict:
    parsed = json.loads(raw)
    return parsed.setdefault("ok", True)                                               # truth-unresolved: external
