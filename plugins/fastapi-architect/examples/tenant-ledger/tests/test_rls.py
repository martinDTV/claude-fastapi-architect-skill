"""Row-level security verified below the application: the app role with and without a
tenant context, and the owner role that bypasses nothing by accident."""

import pytest
from sqlalchemy import text

from tests.conftest import make_account

pytestmark = pytest.mark.anyio


async def test_app_role_sees_only_the_tenant_in_context(client, app_engine, acme, globex):
    await make_account(client, acme["id"], "alice", "acme-1")
    await make_account(client, globex["id"], "carol", "globex-1")
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": acme["id"]})
        names = (await conn.execute(text("SELECT name FROM accounts ORDER BY name"))).scalars().all()
        assert "acme-1" in names and "globex-1" not in names
        await conn.rollback()  # ends the transaction: the context disappears with it
        names = (await conn.execute(text("SELECT name FROM accounts"))).scalars().all()
        assert names == [], "without tenant context the policy yields no rows (fail closed)"


async def test_app_role_cannot_write_rows_for_another_tenant(client, app_engine, acme, globex):
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": acme["id"]})
        with pytest.raises(Exception) as exc:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, tenant_id, name) VALUES (gen_random_uuid(), :other, 'smuggled')"
                ),
                {"other": globex["id"]},
            )
        assert "row-level security" in str(exc.value).lower()
        await conn.rollback()


async def test_owner_role_is_not_subject_to_policies_and_must_never_serve_requests(
    client, owner_engine, acme, globex
):
    await make_account(client, acme["id"], "alice", "acme-2")
    await make_account(client, globex["id"], "carol", "globex-2")
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM accounts WHERE name IN ('acme-2', 'globex-2')"))
        ).scalar_one()
    assert count == 2, "the owner sees everything: that is why the application connects as ledger_app"
