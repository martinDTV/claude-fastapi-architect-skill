"""Transaction boundary: a failure after the balance update rolls everything back."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.core.db import set_tenant_context
from ledger.modules.ledger import repository, service
from ledger.modules.tenants.public import Principal, TenantContext
from tests.conftest import h, make_account

pytestmark = pytest.mark.anyio


async def test_failure_after_balance_update_rolls_back_the_balance(client, app_engine, acme, monkeypatch):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}"
    assert (await client.post(f"{url}/deposits", json={"amount": 40}, headers=h("alice"))).status_code == 201

    async def exploding_add_entry(*args, **kwargs):
        raise RuntimeError("simulated failure after apply_delta")

    monkeypatch.setattr(repository, "add_entry", exploding_add_entry)
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    ctx = TenantContext(
        tenant_id=uuid.UUID(acme["id"]), principal=Principal(uuid.uuid4(), "alice"), role="owner"
    )
    async with factory() as session:
        await set_tenant_context(session, ctx.tenant_id)
        with pytest.raises(RuntimeError):
            await service.move(session, ctx, uuid.UUID(account["id"]), 25, "deposit", None)
    # the session context manager rolled back: balance and entries unchanged
    read = await client.get(url, headers=h("alice"))
    assert read.json()["balance"] == 40 and read.json()["version"] == 2
    entries = await client.get(f"{url}/entries", headers=h("alice"))
    assert len(entries.json()) == 1


async def test_overdraft_leaves_no_entry_behind(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}"
    denied = await client.post(f"{url}/withdrawals", json={"amount": 1}, headers=h("alice"))
    assert denied.status_code == 409 and denied.json()["detail"] == "Insufficient funds"
    assert (await client.get(f"{url}/entries", headers=h("alice"))).json() == []
    assert (await client.get(url, headers=h("alice"))).json()["version"] == 1
