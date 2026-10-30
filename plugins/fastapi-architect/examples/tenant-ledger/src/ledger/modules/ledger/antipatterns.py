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


# --------------------------------------------------------------------------- #
# v1.6 adversarial scenarios (tests/test_adversarial.py): each defect next to its fix
# --------------------------------------------------------------------------- #

import asyncio  # noqa: E402

from fastapi import BackgroundTasks  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from ledger.core.db import SessionDep, set_tenant_context  # noqa: E402
from ledger.modules.ledger import repository  # noqa: E402


async def _unit(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, amount: int, pause: float, commit: bool
) -> None:
    """One unit of work: write, do something else for a while, then commit or roll back."""
    await repository.apply_delta(session, tenant_id, account_id, amount)
    await asyncio.sleep(pause)
    if commit:
        await session.commit()
    else:
        await session.rollback()


async def shared_session_gather(session: AsyncSession, tenant_id: UUID, account_id: UUID) -> None:
    """Two units of work on one AsyncSession, scheduled together: they share one transaction. The
    unit that fails and rolls back discards the other unit's write; the unit that commits would
    publish the other's half-done work. (asyncpg serialises the statements with a lock, so the
    overlap does not raise: the damage is silent.)"""
    await asyncio.gather(  # audit: SA_SESSION_SHARED_CONCURRENTLY
        _unit(session, tenant_id, account_id, 1, pause=0.2, commit=False),  # fails late, rolls back
        _unit(session, tenant_id, account_id, 5, pause=0.4, commit=True),  # succeeds, commits
    )


async def session_per_task(
    factory: async_sessionmaker[AsyncSession], tenant_id: UUID, account_id: UUID
) -> None:
    """The fix: every task opens its own session and sets its own tenant context."""

    async def one(amount: int, pause: float, commit: bool) -> None:
        async with factory() as session:
            await set_tenant_context(session, tenant_id)
            await _unit(session, tenant_id, account_id, amount, pause, commit)

    await asyncio.gather(one(1, 0.2, False), one(5, 0.4, True))


async def record_entry_later(session: AsyncSession, tenant_id: UUID, account_id: UUID) -> None:
    """A task body that writes through whatever session it was handed."""
    await repository.add_entry(session, tenant_id, account_id, 0, "deposit")
    await session.commit()


async def handler_shape_with_request_session(
    background_tasks: BackgroundTasks, session: SessionDep, tenant_id: UUID, account_id: UUID
) -> dict:
    """The shape of a handler that hands its request-scoped session to a background task. The
    dependency closes the session when the response is sent; the task then runs on a session whose
    transaction-local tenant context is gone, and row-level security refuses the write."""
    background_tasks.add_task(
        record_entry_later, session, tenant_id, account_id
    )  # audit: BG_REQUEST_SCOPED_RESOURCE
    return {"scheduled": True}


async def record_entry_with_own_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: UUID, account_id: UUID
) -> None:
    """The fix: the task receives identifiers and opens its own session and context."""
    async with factory() as session:
        await set_tenant_context(session, tenant_id)
        await repository.add_entry(session, tenant_id, account_id, 0, "deposit")
        await session.commit()


async def slow_deposit(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, amount: int, pause: float
) -> None:
    """Applies the movement, then waits (an external call in real code) before committing. Cancelled
    by a timeout during the wait, nothing must remain committed."""
    await repository.apply_delta(session, tenant_id, account_id, amount)
    await asyncio.sleep(pause)
    await session.commit()


# --------------------------------------------------------------------------- #
# v1.7 adversarial scenario (tests/test_tenant_binding.py): RLS context from the requested tenant
# --------------------------------------------------------------------------- #

from fastapi import APIRouter  # noqa: E402
from sqlalchemy import select  # noqa: E402, F811

from ledger.modules.ledger.models import Account as _Account  # noqa: E402
from ledger.modules.tenants.public import Principal, PrincipalDep, require_member  # noqa: E402

unbound_router = APIRouter()  # never included in the application


@unbound_router.get("/tenants/{tenant_id}/accounts-unbound")
async def list_accounts_unbound(tenant_id: UUID, principal: PrincipalDep, session: SessionDep) -> list[str]:
    """The shape of the defect: authenticated, a tenant check that is real (the RLS context is set)
    but fed from the URL, never bound to the principal. The policies then show whatever tenant the
    caller names."""
    await set_tenant_context(session, tenant_id)  # audit: SEC_RLS_CONTEXT_FROM_REQUEST
    rows = await session.scalars(
        select(_Account).where(_Account.tenant_id == tenant_id).order_by(_Account.name)
    )
    return [row.name for row in rows]


async def list_accounts_bound(session: AsyncSession, principal: Principal, tenant_id: UUID) -> list[str]:
    """The fix: the real membership dependency binds tenant and principal (404 for non-members) and
    only then sets the context."""
    await require_member(tenant_id, principal, session)
    rows = await session.scalars(
        select(_Account).where(_Account.tenant_id == tenant_id).order_by(_Account.name)
    )
    return [row.name for row in rows]
