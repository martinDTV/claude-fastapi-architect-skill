"""Three defects with an automatic remediation recipe (v1.9 vertical slice). tests/test_defects.py
observes each defect against a faithful fake session; the tests fail on this code and pass once the
recipes have been applied in an isolated copy."""

from sqlalchemy.ext.asyncio import AsyncSession


async def import_items(session: AsyncSession, items: list) -> None:
    """One transaction per item: a failure half-way leaves earlier items committed."""
    for item in items:
        session.add(item)
        await session.commit()


async def reprice(session: AsyncSession, item, adjustment) -> None:
    """commit() inside begin(): the next statement raises on SQLAlchemy 2.0."""
    async with session.begin():
        session.add(item)
        await session.commit()
        session.add(adjustment)


async def audit_trail(session: AsyncSession, message: str) -> None:
    """The coroutine is never awaited: nothing is executed."""
    session.execute(message)


async def summary(session: AsyncSession, items: list) -> int:
    """A control: already correct, nothing to remediate."""
    for item in items:
        session.add(item)
    await session.commit()
    return len(items)
