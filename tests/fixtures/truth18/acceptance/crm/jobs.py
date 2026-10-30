import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Contact


async def import_contacts(session: AsyncSession, contacts: list[Contact]) -> None:
    for contact in contacts:
        session.add(contact)
        await session.commit()                                                         # truth: TX_COMMIT_IN_LOOP loop_kind=per_item


async def import_in_pages(session: AsyncSession) -> None:
    offset = 0
    while True:
        page = (await session.execute(text("select email from staging order by email offset :o limit 200"), {"o": offset})).scalars().all()
        if not page:
            break
        for email in page:
            session.add(Contact(email))
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=paginated
        offset += 200


async def import_buffered(session: AsyncSession, contacts: list[Contact]) -> None:
    pending: list[Contact] = []
    for contact in contacts:
        pending.append(contact)
        if len(pending) >= 50:
            session.add_all(pending)
            await session.commit()                                                     # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=batch
            pending = []
    if pending:
        session.add_all(pending)
        await session.commit()


async def settle(session: AsyncSession, contact_id: int) -> None:
    for _round in range(5):
        before = await session.scalar(text("select score from contacts where id = :id"), {"id": contact_id})
        await session.execute(text("update contacts set score = score / 2 where id = :id"), {"id": contact_id})
        await session.commit()                                                         # truth-absent: TX_COMMIT_IN_LOOP  loop_kind=convergence
        after = await session.scalar(text("select score from contacts where id = :id"), {"id": contact_id})
        if after == before:
            break


async def save_with_retry(session: AsyncSession, contact: Contact) -> None:
    for attempt in range(3):                                                           # truth-absent: TX_COMMIT_IN_LOOP
        try:
            session.add(contact)
            await session.commit()
            return
        except Exception:
            await session.rollback()
            await asyncio.sleep(0.01)


async def wrong_commit(session: AsyncSession, contacts: list[Contact]) -> None:
    async with session.begin():
        session.add_all(contacts)
        await session.commit()                                                         # truth: TX_COMMIT_INSIDE_BEGIN_BLOCK
