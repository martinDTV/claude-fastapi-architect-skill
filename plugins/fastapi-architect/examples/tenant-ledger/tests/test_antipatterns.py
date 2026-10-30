"""The defects the static auditor can only suspect, reproduced for real, next to their fixes."""

import uuid

import pytest
from sqlalchemy.exc import InvalidRequestError, MissingGreenlet
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.core.db import set_tenant_context
from ledger.modules.ledger import repository
from ledger.modules.ledger.antipatterns import (
    commit_inside_begin,
    commit_per_item_inside_begin,
    deltas_in_one_block,
    entries_eager,
    entries_lazy,
    naive_deposit,
)
from tests.conftest import h, make_account

pytestmark = pytest.mark.anyio


async def _balance(client, tenant_id: str, account_id: str) -> int:
    return (
        await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}", headers=h("alice"))
    ).json()["balance"]


async def test_read_modify_write_loses_an_update_and_the_atomic_update_does_not(client, app_engine, acme):
    account = await make_account(client, acme["id"], "alice")
    assert (
        await client.post(
            f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits",
            json={"amount": 100},
            headers=h("alice"),
        )
    ).status_code == 201
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    # Two sessions interleaved deterministically: both read 100, then both write.
    async with factory() as s1, factory() as s2:
        await set_tenant_context(s1, tenant)
        await set_tenant_context(s2, tenant)
        a1 = await s1.get(repository.Account, aid)
        a2 = await s2.get(repository.Account, aid)
        a1.balance += 10
        await s1.commit()
        a2.balance += 10
        await s2.commit()
    assert await _balance(client, acme["id"], account["id"]) == 110, (
        "second writer overwrote the first: one deposit lost"
    )

    # The same interleaving through naive_deposit, for the record.
    async with factory() as s:
        await set_tenant_context(s, tenant)
        await naive_deposit(s, aid, 0)

    # The fix: the database computes the new balance.
    async with factory() as s1, factory() as s2:
        await set_tenant_context(s1, tenant)
        await set_tenant_context(s2, tenant)
        await repository.apply_delta(s1, tenant, aid, 10)
        await s1.commit()
        await repository.apply_delta(s2, tenant, aid, 10)
        await s2.commit()
    assert await _balance(client, acme["id"], account["id"]) == 130, "both atomic updates survived"


async def test_implicit_lazy_load_raises_missing_greenlet_and_selectinload_fixes_it(client, app_engine, acme):
    account = await make_account(client, acme["id"], "alice")
    assert (
        await client.post(
            f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits",
            json={"amount": 1},
            headers=h("alice"),
        )
    ).status_code == 201
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session:
        await set_tenant_context(session, tenant)
        with pytest.raises(MissingGreenlet):
            await entries_lazy(session, aid)
    async with factory() as session:
        await set_tenant_context(session, tenant)
        assert await entries_eager(session, aid) == 1


@pytest.mark.antipattern_demo
async def test_commit_inside_begin_block_raises_on_the_next_statement(client, app_engine, acme):
    """TX_COMMIT_INSIDE_BEGIN_BLOCK, reproduced on PostgreSQL with SQLAlchemy 2.0: the inner commit
    closes the block's transaction; the statement after it raises InvalidRequestError, so the first
    delta is committed and the second never happens. The loop shape fails on the second iteration.
    Without the inner commit both deltas are committed together on exit."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    start = await _balance(client, acme["id"], account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    with pytest.raises(InvalidRequestError, match="closed transaction inside context manager"):
        async with factory() as session:
            await commit_inside_begin(session, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == start + 1
    with pytest.raises(InvalidRequestError, match="closed transaction inside context manager"):
        async with factory() as session:
            await commit_per_item_inside_begin(session, tenant, aid, [1, 1])
    assert (
        await _balance(client, acme["id"], account["id"]) == start + 2
    )  # first iteration committed, second raised
    async with factory() as session:
        await deltas_in_one_block(session, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == start + 4
