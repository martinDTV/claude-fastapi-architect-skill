from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from ledger.core.db import SessionDep
from ledger.modules.ledger import repository, service
from ledger.modules.ledger.schemas import (
    AccountCreate,
    AccountRead,
    AccountRename,
    EntryRead,
    MovementCreate,
    MovementRead,
)
from ledger.modules.tenants.public import MemberDep, OwnerDep

router = APIRouter(prefix="/tenants/{tenant_id}/accounts", tags=["ledger"])


def _read(account) -> AccountRead:
    return AccountRead(id=account.id, name=account.name, balance=account.balance, version=account.version)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate, ctx: MemberDep, session: SessionDep, response: Response
) -> AccountRead:
    account = await service.create_account(session, ctx, payload.name)
    response.headers["ETag"] = service.etag_for(account.version)
    return _read(account)


@router.get("")
async def list_accounts(ctx: MemberDep, session: SessionDep) -> list[AccountRead]:
    return [_read(a) for a in await repository.list_accounts(session, ctx.tenant_id)]


@router.get("/{account_id}")
async def get_account(
    account_id: UUID, ctx: MemberDep, session: SessionDep, response: Response
) -> AccountRead:
    account = await service.get_account(session, ctx, account_id)
    response.headers["ETag"] = service.etag_for(account.version)
    return _read(account)


@router.patch("/{account_id}")
async def rename_account(
    account_id: UUID,
    payload: AccountRename,
    ctx: MemberDep,
    session: SessionDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AccountRead:
    account = await service.rename(session, ctx, account_id, payload.name, if_match)
    response.headers["ETag"] = service.etag_for(account.version)
    return _read(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: UUID, ctx: OwnerDep, session: SessionDep) -> None:
    await service.delete_account(session, ctx, account_id)


@router.post("/{account_id}/deposits", status_code=status.HTTP_201_CREATED)
async def deposit(
    account_id: UUID,
    payload: MovementCreate,
    ctx: MemberDep,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MovementRead:
    return MovementRead(
        **await service.move(session, ctx, account_id, payload.amount, "deposit", idempotency_key)
    )


@router.post("/{account_id}/withdrawals", status_code=status.HTTP_201_CREATED)
async def withdraw(
    account_id: UUID,
    payload: MovementCreate,
    ctx: MemberDep,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MovementRead:
    return MovementRead(
        **await service.move(session, ctx, account_id, payload.amount, "withdrawal", idempotency_key)
    )


@router.get("/{account_id}/entries")
async def list_entries(account_id: UUID, ctx: MemberDep, session: SessionDep) -> list[EntryRead]:
    await service.get_account(session, ctx, account_id)
    return [
        EntryRead(id=e.id, kind=e.kind, amount=e.amount)
        for e in await repository.list_entries(session, ctx.tenant_id, account_id)
    ]
