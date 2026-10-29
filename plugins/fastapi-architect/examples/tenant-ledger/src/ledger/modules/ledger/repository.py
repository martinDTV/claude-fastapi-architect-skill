"""Queries only. No business rules, no commits. Every statement also filters by tenant_id
explicitly: RLS is the second line of defence, not the only one."""

from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.modules.ledger.models import Account, Entry, IdempotencyKey


async def create_account(session: AsyncSession, tenant_id: UUID, name: str) -> Account:
    account = Account(tenant_id=tenant_id, name=name)
    session.add(account)
    await session.flush()
    return account


async def get_account(session: AsyncSession, tenant_id: UUID, account_id: UUID) -> Account | None:
    return await session.scalar(
        select(Account).where(Account.id == account_id, Account.tenant_id == tenant_id)
    )


async def list_accounts(session: AsyncSession, tenant_id: UUID) -> list[Account]:
    return list(
        (
            await session.scalars(
                select(Account).where(Account.tenant_id == tenant_id).order_by(Account.name)
            )
        ).all()
    )


async def apply_delta(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, amount: int
) -> tuple[int, int] | None:
    """Atomic balance change: the database computes balance + amount, so two concurrent
    movements cannot lose each other, and the CHECK constraint rejects overdrafts."""
    row = (
        await session.execute(
            update(Account)
            .where(Account.id == account_id, Account.tenant_id == tenant_id)
            .values(balance=Account.balance + amount, version=Account.version + 1)
            .returning(Account.balance, Account.version)
            .execution_options(synchronize_session=False)
        )
    ).first()
    return (row.balance, row.version) if row else None


async def rename_if_version(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, name: str, expected_version: int
) -> Account | None:
    """Optimistic concurrency: the UPDATE only applies when the version still matches.

    RETURNING gives back the updated row inside the same transaction (the RLS tenant context
    is transaction-local, so a read after commit would see nothing). synchronize_session=False:
    we return the fresh row instead of asking the ORM to reconcile its identity map.
    """
    stmt = (
        update(Account)
        .where(Account.id == account_id, Account.tenant_id == tenant_id, Account.version == expected_version)
        .values(name=name, version=Account.version + 1)
        .returning(Account)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_entry(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, amount: int, kind: str
) -> Entry:
    entry = Entry(tenant_id=tenant_id, account_id=account_id, amount=amount, kind=kind)
    session.add(entry)
    await session.flush()
    return entry


async def list_entries(session: AsyncSession, tenant_id: UUID, account_id: UUID) -> list[Entry]:
    stmt = (
        select(Entry)
        .where(Entry.tenant_id == tenant_id, Entry.account_id == account_id)
        .order_by(Entry.created_at, Entry.id)
    )
    return list((await session.scalars(stmt)).all())


async def delete_account(session: AsyncSession, tenant_id: UUID, account_id: UUID) -> int:
    result = await session.execute(
        delete(Account).where(Account.id == account_id, Account.tenant_id == tenant_id)
    )
    return result.rowcount


async def get_idempotent(session: AsyncSession, tenant_id: UUID, key: str) -> dict | None:
    return await session.scalar(
        select(IdempotencyKey.response).where(
            IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key
        )
    )


async def store_idempotent(session: AsyncSession, tenant_id: UUID, key: str, response: dict) -> None:
    await session.execute(insert(IdempotencyKey).values(tenant_id=tenant_id, key=key, response=response))
