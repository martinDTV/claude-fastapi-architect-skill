"""P4: commits inside loops, classified by structure, never by the name of a constant."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .repo import Account

BATCH = 100
PAGE = 500


async def per_item(session: AsyncSession, items: list[Account]) -> None:
    for item in items:
        session.add(item)
        await session.commit()                                                         # truth: TX_COMMIT_IN_LOOP loop_kind=per_item


async def batched_by_modulo(session: AsyncSession, items: list[Account]) -> None:
    for index, item in enumerate(items, start=1):
        session.add(item)
        if index % BATCH == 0:
            await session.commit()                                                     # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=batch
    await session.commit()


async def batched_by_buffer(session: AsyncSession, items: list[Account]) -> None:
    buffer: list[Account] = []
    for item in items:
        buffer.append(item)
        if len(buffer) >= BATCH:
            session.add_all(buffer)
            await session.commit()                                                     # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=batch
            buffer.clear()


async def named_only(session: AsyncSession, items: list[Account]) -> None:
    for item in items:
        BATCH_SIZE = 10  # noqa: N806  a constant with a batch-like name proves nothing
        session.add(item)
        await session.commit()                                                         # truth: TX_COMMIT_IN_LOOP loop_kind=per_item


async def paginated(session: AsyncSession) -> int:
    processed = 0
    cursor = 0
    while True:
        rows = (await session.execute(text("select id from accounts where id > :c order by id limit :l"), {"c": cursor, "l": PAGE})).scalars().all()
        if not rows:
            break
        await session.execute(text("update accounts set checked = true where id = any(:ids)"), {"ids": rows})
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=paginated
        cursor = rows[-1]
        processed += len(rows)
    return processed


async def convergence(session: AsyncSession, account_id: int) -> str:
    outcome = "unchanged"
    for _ in range(3):
        before = await session.scalar(text("select version from accounts where id = :id"), {"id": account_id})
        await session.execute(text("update accounts set synced = true where id = :id"), {"id": account_id})
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=convergence
        after = await session.scalar(text("select version from accounts where id = :id"), {"id": account_id})
        if before == after:
            break
        outcome = "changed"
    return outcome


async def transactional_retry(session: AsyncSession, account: Account) -> None:
    for attempt in range(3):                                                           # truth-absent: TX_COMMIT_IN_LOOP
        try:
            session.add(account)
            await session.commit()
            break
        except Exception:
            await session.rollback()
            await asyncio.sleep(0)


async def commit_inside_begin(session: AsyncSession, items: list[Account]) -> None:
    async with session.begin():
        for item in items:
            session.add(item)
            await session.commit()                                                     # truth: TX_COMMIT_INSIDE_BEGIN_BLOCK


async def flush_inside_begin(session: AsyncSession, items: list[Account]) -> None:
    async with session.begin():
        for item in items:
            session.add(item)
            await session.flush()                                                      # truth-absent: TX_COMMIT_INSIDE_BEGIN_BLOCK
