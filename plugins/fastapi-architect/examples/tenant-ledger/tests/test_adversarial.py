"""Adversarial scenarios the static auditor can only suspect, reproduced on real PostgreSQL:
a shared AsyncSession under concurrency, a request-scoped session that outlives its scope,
cancellation by timeout in the middle of a unit of work, a lost response retried with and
without an idempotency key, and the row-level-security configuration invariants."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.core.db import set_tenant_context
from ledger.modules.ledger.antipatterns import (
    record_entry_later,
    record_entry_with_own_session,
    session_per_task,
    shared_session_gather,
    slow_deposit,
)
from tests.conftest import APP_ROLE, h, make_account

pytestmark = pytest.mark.anyio


async def _balance(client, tenant_id: str, account_id: str) -> int:
    read = await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}", headers=h("alice"))
    return read.json()["balance"]


async def _entries(client, tenant_id: str, account_id: str) -> int:
    read = await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}/entries", headers=h("alice"))
    return len(read.json())


async def test_one_session_shared_by_two_tasks_mixes_their_transactions(client, app_engine, acme):
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    async with factory() as session:
        await set_tenant_context(session, tenant)
        await shared_session_gather(session, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == 0, (
        "the unit that rolled back discarded the other unit's +5: one session means one transaction"
    )

    await session_per_task(factory, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == 5, (
        "own sessions: the rollback touched only its own unit"
    )


async def test_request_session_handed_to_a_task_loses_its_context_after_the_response(
    client, app_engine, acme
):
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    # the request: the dependency opens the session and the handler sets the tenant context ...
    async with factory() as request_session:
        await set_tenant_context(request_session, tenant)
    # ... the response is sent, the dependency closes the session, and only then the task runs
    with pytest.raises(SQLAlchemyError) as exc:
        await record_entry_later(request_session, tenant, aid)
    assert "row-level security" in str(exc.value).lower(), "no tenant context: the policy refuses the write"
    await request_session.rollback()
    assert await _entries(client, acme["id"], account["id"]) == 0

    # the fix: the task opens its own session and sets the context from the identifiers it received
    await record_entry_with_own_session(factory, tenant, aid)
    assert await _entries(client, acme["id"], account["id"]) == 1


async def test_cancellation_by_timeout_leaves_nothing_committed_and_the_connection_usable(
    client, app_engine, acme
):
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    async with factory() as session:
        await set_tenant_context(session, tenant)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow_deposit(session, tenant, aid, 50, pause=5.0), timeout=0.2)
        await session.rollback()  # the cancelled unit of work is discarded explicitly
    assert await _balance(client, acme["id"], account["id"]) == 0, (
        "the UPDATE before the cancellation did not survive"
    )

    async with factory() as session:  # the pool and the session are still healthy afterwards
        await set_tenant_context(session, tenant)
        await slow_deposit(session, tenant, aid, 7, pause=0.0)
    assert await _balance(client, acme["id"], account["id"]) == 7


async def test_lost_response_retried_without_a_key_duplicates_and_with_a_key_does_not(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits"

    # without a key: the client never saw the first response and retries -> two movements
    await client.post(url, json={"amount": 10}, headers=h("alice"))  # response "lost"
    retry = await client.post(url, json={"amount": 10}, headers=h("alice"))
    assert retry.status_code == 201
    assert await _balance(client, acme["id"], account["id"]) == 20, "the retry applied the deposit again"

    # with a key: the retry replays the stored result; exactly one movement
    key = uuid.uuid4().hex
    await client.post(url, json={"amount": 10}, headers={**h("alice"), "Idempotency-Key": key})  # "lost"
    retry = await client.post(url, json={"amount": 10}, headers={**h("alice"), "Idempotency-Key": key})
    assert retry.status_code == 201
    assert await _balance(client, acme["id"], account["id"]) == 30
    assert await _entries(client, acme["id"], account["id"]) == 3


async def test_rls_configuration_invariants_hold(owner_engine):
    """What makes the isolation tests meaningful: the application role cannot bypass policies,
    owns no table, and every tenant table forces row-level security even for its owner."""
    async with owner_engine.connect() as conn:
        flags = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = :r"),
                {"r": APP_ROLE},
            )
        ).one()
        assert tuple(flags) == (False, False, False)
        owners = (
            await conn.execute(
                text(
                    "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
        ).all()
        assert owners and all(owner != APP_ROLE for _table, owner in owners)
        forced = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('accounts', 'entries', 'idempotency_keys') ORDER BY relname"
                )
            )
        ).all()
        assert [(name, True, True) for name, _e, _f in forced] == [tuple(row) for row in forced]
        assert len(forced) == 3
