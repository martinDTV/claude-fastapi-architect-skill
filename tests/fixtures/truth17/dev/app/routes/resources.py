"""Lifecycle of coroutines, locks, HTTP clients and sessions."""

import asyncio
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import AsyncSessionLocal, RequestContext, SessionDep, get_context
from ..services import accounts

router = APIRouter()
LOCK = asyncio.Lock()
shared_client = httpx.AsyncClient(base_url="https://api.invalid", timeout=5.0)
background: set = set()


@router.get("/summary")
async def summary(ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:
    report = accounts.compute_summary(session)                                       # truth: ASYNC_COROUTINE_NOT_AWAITED
    return {"report": report}


@router.get("/summary-returned")
async def summary_returned(ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:
    return accounts.compute_summary(session)                                         # truth: ASYNC_COROUTINE_NOT_AWAITED


def summary_sync_wrapper(session: AsyncSession):
    return accounts.compute_summary(session)                                         # truth-absent: ASYNC_COROUTINE_NOT_AWAITED


@router.get("/summary-ok")
async def summary_ok(ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:
    return await accounts.compute_summary(session)                                   # truth-absent: ASYNC_COROUTINE_NOT_AWAITED


async def summaries_gathered() -> list:
    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        return list(await asyncio.gather(accounts.compute_summary(s1), accounts.compute_summary(s2)))  # truth-absent: ASYNC_COROUTINE_NOT_AWAITED


async def summary_in_task() -> None:
    async with AsyncSessionLocal() as session:
        task = asyncio.create_task(accounts.compute_summary(session))                 # truth-absent: ASYNC_COROUTINE_NOT_AWAITED
        background.add(task)
        await task


async def transfer_with_local_lock(session: AsyncSession, src: UUID, dst: UUID) -> None:
    lock = asyncio.Lock()                                                             # truth: ASYNC_LOCK_PER_CALL
    async with lock:
        await session.execute(accounts.text("update accounts set balance = balance - 1 where id = :id"), {"id": src})
        await session.commit()


async def transfer_with_shared_lock(session: AsyncSession, src: UUID, dst: UUID) -> None:
    async with LOCK:                                                                  # truth-absent: ASYNC_LOCK_PER_CALL
        await session.execute(accounts.text("update accounts set balance = balance - 1 where id = :id"), {"id": src})
        await session.commit()


@router.get("/rates")
async def rates(ctx: RequestContext = Depends(get_context)) -> dict:
    client = httpx.AsyncClient(timeout=5.0)                                           # truth: REL_HTTP_CLIENT_PER_CALL
    return (await client.get("https://rates.invalid/latest")).json()


@router.get("/rates-managed")
async def rates_managed(ctx: RequestContext = Depends(get_context)) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:                              # truth-absent: REL_HTTP_CLIENT_PER_CALL
        return (await client.get("https://rates.invalid/latest")).json()


@router.get("/rates-shared")
async def rates_shared(ctx: RequestContext = Depends(get_context)) -> dict:
    a, b = await asyncio.gather(shared_client.get("/a"), shared_client.get("/b"))     # truth-absent: SA_SESSION_SHARED_CONCURRENTLY
    return {"a": a.json(), "b": b.json()}                                             # truth-absent: REL_HTTP_CLIENT_PER_CALL


@router.get("/rates-closed")
async def rates_closed(ctx: RequestContext = Depends(get_context)) -> dict:
    client = httpx.AsyncClient(timeout=5.0)                                           # truth-absent: REL_HTTP_CLIENT_PER_CALL
    try:
        return (await client.get("https://rates.invalid/latest")).json()
    finally:
        await client.aclose()


async def both_summaries_one_session(session: AsyncSession) -> list:
    return list(await asyncio.gather(accounts.compute_summary(session), accounts.compute_summary(session)))  # truth: SA_SESSION_SHARED_CONCURRENTLY


async def both_summaries_two_sessions() -> list:
    async def one() -> dict:
        async with AsyncSessionLocal() as session:
            return await accounts.compute_summary(session)

    return list(await asyncio.gather(one(), one()))                                   # truth-absent: SA_SESSION_SHARED_CONCURRENTLY
