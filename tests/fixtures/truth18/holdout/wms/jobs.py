"""P4: loop commits by structure; deliberate loops must not hide other transactional defects."""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Location


async def per_item(session: AsyncSession, locations: list[Location]) -> None:
    for location in locations:
        session.add(location)
        await session.commit()                                                         # truth: TX_COMMIT_IN_LOOP loop_kind=per_item


async def counted_batches(session: AsyncSession, locations: list[Location]) -> None:
    pending = 0
    for location in locations:
        session.add(location)
        pending += 1
        if pending >= 200:
            await session.commit()                                                     # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=batch
            pending = 0
    await session.commit()


async def pages(session: AsyncSession) -> None:
    for page in range(10_000):
        rows = (await session.execute(text("select id from moves limit 100 offset :o"), {"o": page * 100})).scalars().all()
        if not rows:
            break
        await session.execute(text("update moves set done = true where id = any(:ids)"), {"ids": rows})
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=paginated


async def settle(session: AsyncSession, move_id: int) -> None:
    while True:
        before = await session.scalar(text("select qty from moves where id = :id"), {"id": move_id})
        await session.execute(text("update moves set qty = qty - 1 where id = :id and qty > 0"), {"id": move_id})
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=convergence
        after = await session.scalar(text("select qty from moves where id = :id"), {"id": move_id})
        if after == before:
            break


async def with_retry(session: AsyncSession, location: Location) -> None:
    for attempt in range(3):                                                           # truth-absent: TX_COMMIT_IN_LOOP
        try:
            session.add(location)
            await session.commit()
            return
        except Exception:
            await session.rollback()
            await asyncio.sleep(0)


async def pages_with_inner_per_item(session: AsyncSession) -> None:
    for page in range(10_000):
        rows = (await session.execute(text("select id from moves limit 100 offset :o"), {"o": page * 100})).scalars().all()
        if not rows:
            break
        for row in rows:
            await session.execute(text("update moves set done = true where id = :id"), {"id": row})
            await session.commit()                                                     # truth: TX_COMMIT_IN_LOOP loop_kind=per_item


async def pages_calling_out(session: AsyncSession, client: httpx.AsyncClient) -> None:
    for page in range(10_000):
        rows = (await session.execute(text("select id from moves limit 100 offset :o"), {"o": page * 100})).scalars().all()
        if not rows:
            break
        async with session.begin_nested():
            await client.post("/notify", json={"ids": rows})                           # truth: TX_EXTERNAL_CALL_IN_TRANSACTION
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=paginated


async def commit_then_more(session: AsyncSession, location: Location) -> None:
    async with session.begin():
        session.add(location)
        await session.commit()                                                         # truth: TX_COMMIT_INSIDE_BEGIN_BLOCK follows=True
        session.add(location)


async def commit_last(session: AsyncSession, location: Location) -> None:
    async with session.begin():
        session.add(location)
        await session.commit()                                                         # truth: TX_COMMIT_INSIDE_BEGIN_BLOCK follows=False


async def flush_only(session: AsyncSession, location: Location) -> None:
    async with session.begin():
        session.add(location)
        await session.flush()                                                          # truth-absent: TX_COMMIT_INSIDE_BEGIN_BLOCK
