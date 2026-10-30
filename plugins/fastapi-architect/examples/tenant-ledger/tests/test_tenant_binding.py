"""A tenant check that is present but not bound to the principal, reproduced on PostgreSQL: the
row-level-security context is set from the tenant named in the URL, so a member of ACME who asks
for Globex's id reads Globex's accounts. The fix binds membership before the context."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.core.errors import NotFound
from ledger.modules.ledger.antipatterns import list_accounts_bound, list_accounts_unbound
from ledger.modules.tenants.public import Principal
from tests.conftest import make_account

pytestmark = pytest.mark.anyio


async def _principal(owner_engine, username: str) -> Principal:
    async with owner_engine.connect() as conn:
        user_id = (
            await conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
        ).scalar_one()
    return Principal(user_id=user_id, username=username)


async def test_rls_context_from_the_requested_tenant_leaks_and_membership_binding_closes_it(
    client, app_engine, owner_engine, acme, globex
):
    await make_account(client, acme["id"], "alice", "acme-ops")
    await make_account(client, globex["id"], "carol", "globex-ops")
    alice = await _principal(owner_engine, "alice")  # member of ACME only
    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    # the defect: authenticated as alice, asking for Globex, the context is set to Globex and RLS
    # (correctly configured, non-owner role, forced) shows Globex's rows: the check is real, the binding is missing
    async with factory() as session:
        names = await list_accounts_unbound(uuid.UUID(globex["id"]), alice, session)
        await session.rollback()
    assert names == ["globex-ops"], "the requested tenant decided what the policies showed"

    # the fix: membership before context; alice is not a member of Globex
    async with factory() as session:
        with pytest.raises(NotFound):
            await list_accounts_bound(session, alice, uuid.UUID(globex["id"]))
        await session.rollback()
    async with factory() as session:
        assert await list_accounts_bound(session, alice, uuid.UUID(acme["id"])) == ["acme-ops"]
        await session.rollback()


async def test_a_tenant_id_alone_never_authorizes_a_background_worker(
    client, app_engine, owner_engine, acme, globex
):
    """RLS cannot know who asked: a worker handed a tenant id writes into that tenant. The binding
    has to happen where the principal is known, before the id is handed over."""
    account = await make_account(client, globex["id"], "carol", "globex-main")
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    from ledger.modules.ledger.antipatterns import record_entry_with_own_session

    await record_entry_with_own_session(factory, uuid.UUID(globex["id"]), uuid.UUID(account["id"]))
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM entries WHERE account_id = :a"), {"a": account["id"]}
            )
        ).scalar_one()
    assert count == 1, (
        "the worker wrote into Globex with nothing but the id: the edge must bind tenant and principal first"
    )
