"""Deliberate anti-patterns, used ONLY by tests/test_antipatterns.py to reproduce the defects
the static auditor reports as suspicions, and to show the fixed version next to each.

Nothing in the application imports this module.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ledger.modules.ledger.models import Account


async def naive_deposit(session: AsyncSession, account_id: UUID, amount: int) -> int:
    """Read-modify-write: two concurrent callers can both read 100 and both write 110."""
    account = await session.get(Account, account_id)
    assert account is not None
    account.balance += amount  # audit: TX_READ_MODIFY_WRITE
    await session.commit()
    return account.balance


async def entries_lazy(session: AsyncSession, account_id: UUID) -> int:
    """Implicit lazy load under AsyncSession: raises MissingGreenlet."""
    account = await session.get(Account, account_id)
    assert account is not None
    return len(account.entries)  # audit: SA_ASYNC_LAZY_LOAD_RISK


async def entries_eager(session: AsyncSession, account_id: UUID) -> int:
    """The fix: an explicit loader option."""
    account = await session.scalar(
        select(Account).options(selectinload(Account.entries)).where(Account.id == account_id)
    )
    assert account is not None
    return len(account.entries)
