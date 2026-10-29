"""Loops that look like retries or per-item commits but are not, and a permission dependency
built with a factory: each of these was a false positive on a real repository."""

import asyncio
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...core import SessionDep, get_context
from ...core.security import RequestContext, User
from .models import Account

BATCH = 50
PERMISSIONS = {"grant": "accounts:grant"}


async def refresh_then_mark(session: AsyncSession, client: httpx.AsyncClient, account: Account) -> dict:
    for attempt in range(2):                                                           # truth-absent: RETRY_NON_IDEMPOTENT_WRITE
        try:
            return (await client.get("/calendars")).json()                             # a read, retried once with a fresh token
        except httpx.HTTPStatusError:
            if attempt == 1:
                account.balance = -1                                                   # written once, on the give-up path only
                session.add(account)
                await session.commit()
                raise
            await asyncio.sleep(0)
    return {}


async def call_with_token_refresh(session: AsyncSession, client: httpx.AsyncClient, token: str) -> httpx.Response:
    for _attempt in range(2):                                                          # truth-absent: TX_COMMIT_IN_LOOP
        try:
            return await client.get("/me", headers={"Authorization": token})
        except httpx.HTTPStatusError:
            token = "fresh"
            await session.commit()                                                     # stores the refreshed token, then retries
            continue
    raise RuntimeError("unreachable")


async def two_pass_purge(client: httpx.AsyncClient) -> int:
    deleted = 0
    for _ in range(2):                                                                 # truth-absent: RETRY_NON_IDEMPOTENT_WRITE
        pending = (await client.get("/events")).json()
        if not pending:
            break
        for event_id in pending:
            try:
                await client.delete(f"/events/{event_id}")                             # per-item error handling, not a retry
            except httpx.HTTPStatusError:
                continue
            deleted += 1
    return deleted


async def backfill(session: AsyncSession, accounts: list[Account]) -> None:
    for index, account in enumerate(accounts, start=1):
        session.add(account)
        if index % BATCH == 0:
            await session.commit()                                                     # truth-absent: TX_COMMIT_IN_LOOP
    await session.commit()


async def retried_write(session: AsyncSession, account: Account) -> None:
    for _attempt in range(3):                                                          # truth: RETRY_NON_IDEMPOTENT_WRITE
        try:
            session.add(account)
            await session.commit()                                                     # the write itself is retried
            break
        except Exception:
            await asyncio.sleep(0)


def require(permission: str):
    """A dependency factory: Depends(require("accounts:grant")) names the policy in its argument."""

    async def dependency(ctx: RequestContext = Depends(get_context)) -> RequestContext:
        if permission not in ctx.user.role:
            raise PermissionError
        return ctx

    return dependency


GrantContext = Annotated[RequestContext, Depends(require(PERMISSIONS["grant"]))]
router = APIRouter()


@router.post("/accounts/{account_id}/grants")
async def grant_access(account_id: UUID, payload: dict, ctx: GrantContext, session: SessionDep = None) -> dict:  # truth-absent: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    user = await session.get(User, payload["user_id"])
    user.role = "granted"
    await session.commit()
    return {"ok": True}
