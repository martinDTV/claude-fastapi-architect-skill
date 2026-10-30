"""Verification targets for scripts/audit_remediate.py (v1.9). Deselected by default (marker ``remediation``):
each test FAILS on the deliberate anti-pattern and PASSES once its recipe has been applied in the isolated
copy, on real PostgreSQL (SQLAlchemy async, asyncpg). The pipeline links one test (or a `-k` group) to one
finding and runs it before the patch (observing the defect) and after it.

    uv run pytest -q -m remediation tests/test_remediation_targets.py
"""

import gc
import uuid
import warnings

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.modules.ledger.antipatterns import (
    commit_inside_begin,
    commit_per_item_inside_begin,
    deposit_each,
    note_without_await,
)
from tests.conftest import h, make_account

pytestmark = [pytest.mark.anyio, pytest.mark.remediation]


async def _balance(client, tenant_id: str, account_id: str) -> int:
    return (
        await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}", headers=h("alice"))
    ).json()["balance"]


async def _entries(client, tenant_id: str, account_id: str) -> int:
    return len(
        (
            await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}/entries", headers=h("alice"))
        ).json()
    )


async def test_commit_inside_begin_is_remediated(client, app_engine, acme):
    """remove_commit_inside_begin (statement after the commit): no InvalidRequestError, both deltas committed."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    start = await _balance(client, acme["id"], account["id"])
    async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
        await commit_inside_begin(session, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == start + 2


async def test_commit_per_item_inside_begin_is_remediated(client, app_engine, acme):
    """remove_commit_inside_begin (loop shape): every amount applied, committed together on exit."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    start = await _balance(client, acme["id"], account["id"])
    async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
        await commit_per_item_inside_begin(session, tenant, aid, [1, 2, 3])
    assert await _balance(client, acme["id"], account["id"]) == start + 6


async def test_note_without_await_executes_exactly_once_without_warnings(client, app_engine, acme):
    """add_await: the insert runs exactly once and no 'coroutine was never awaited' warning is emitted."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    before = await _entries(client, acme["id"], account["id"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
            await note_without_await(session, tenant, aid, 7)
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]
    assert await _entries(client, acme["id"], account["id"]) == before + 1


async def test_note_without_await_surfaces_database_errors_after_the_fix(client, app_engine, acme):
    """Documented behaviour change of add_await: an insert that violates a constraint now raises at the
    call site instead of silently never running."""
    tenant = uuid.UUID(acme["id"])
    with pytest.raises(DBAPIError):
        async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
            await note_without_await(session, tenant, uuid.uuid4(), 1)  # no such account: foreign key


async def test_deposit_each_is_atomic_when_an_amount_is_rejected(client, app_engine, acme):
    """move_commit_after_loop: a rejected amount (CHECK balance >= 0) leaves nothing committed."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    start = await _balance(client, acme["id"], account["id"])
    with pytest.raises(DBAPIError):
        async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
            await deposit_each(session, tenant, aid, [5, 5, -1000])
    assert await _balance(client, acme["id"], account["id"]) == start
