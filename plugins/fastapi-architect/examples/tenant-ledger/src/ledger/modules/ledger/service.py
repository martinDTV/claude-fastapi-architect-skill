"""Business rules and the transaction boundary: one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.errors import Conflict, NotFound, PreconditionFailed, PreconditionRequired
from ledger.modules.ledger import repository
from ledger.modules.ledger.models import Account
from ledger.modules.tenants.public import TenantContext


def etag_for(version: int) -> str:
    return f'"{version}"'


def version_from_etag(if_match: str | None) -> int:
    if not if_match:
        raise PreconditionRequired("If-Match header with the account ETag is required")
    raw = if_match.strip().removeprefix("W/").strip('"')
    if not raw.isdigit():
        raise PreconditionFailed("Malformed If-Match")
    return int(raw)


async def create_account(session: AsyncSession, ctx: TenantContext, name: str) -> Account:
    account = await repository.create_account(session, ctx.tenant_id, name)
    await session.commit()
    return account


async def get_account(session: AsyncSession, ctx: TenantContext, account_id: UUID) -> Account:
    account = await repository.get_account(session, ctx.tenant_id, account_id)
    if account is None:
        raise NotFound("Account not found")
    return account


async def move(
    session: AsyncSession,
    ctx: TenantContext,
    account_id: UUID,
    amount: int,
    kind: str,
    idempotency_key: str | None,
) -> dict:
    """Deposit or withdrawal. Idempotent when the client sends Idempotency-Key: a replay
    (after a lost response) returns the stored result instead of applying the movement twice."""
    if idempotency_key:
        stored = await repository.get_idempotent(session, ctx.tenant_id, idempotency_key)
        if stored is not None:
            return stored
    delta = amount if kind == "deposit" else -amount
    try:
        result = await repository.apply_delta(session, ctx.tenant_id, account_id, delta)
    except IntegrityError as exc:  # CHECK balance >= 0
        await session.rollback()
        raise Conflict("Insufficient funds") from exc
    if result is None:
        await session.rollback()
        raise NotFound("Account not found")
    balance, version = result
    entry = await repository.add_entry(session, ctx.tenant_id, account_id, amount, kind)
    response = {
        "account_id": str(account_id),
        "entry_id": str(entry.id),
        "kind": kind,
        "amount": amount,
        "balance": balance,
        "version": version,
    }
    if idempotency_key:
        try:
            await repository.store_idempotent(session, ctx.tenant_id, idempotency_key, response)
        except IntegrityError:
            # a concurrent request with the same key won the race: discard our movement, return theirs
            await session.rollback()
            stored = await repository.get_idempotent(session, ctx.tenant_id, idempotency_key)
            if stored is None:  # pragma: no cover - defensive
                raise
            return stored
    await session.commit()
    return response


async def rename(
    session: AsyncSession, ctx: TenantContext, account_id: UUID, name: str, if_match: str | None
) -> Account:
    expected = version_from_etag(if_match)
    account = await repository.rename_if_version(session, ctx.tenant_id, account_id, name, expected)
    if account is None:
        current = await repository.get_account(session, ctx.tenant_id, account_id)
        # read the version before rollback: rollback expires loaded instances and a later
        # attribute access would try to refresh outside the async context (MissingGreenlet)
        current_version = current.version if current is not None else None
        await session.rollback()
        if current_version is None:
            raise NotFound("Account not found")
        raise PreconditionFailed(f"Account changed: current ETag is {etag_for(current_version)}")
    await session.commit()
    return account


async def delete_account(session: AsyncSession, ctx: TenantContext, account_id: UUID) -> None:
    if await repository.delete_account(session, ctx.tenant_id, account_id) == 0:
        await session.rollback()
        raise NotFound("Account not found")
    await session.commit()
