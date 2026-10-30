"""Verification targets for scripts/audit_remediate.py (v1.9). Deselected by default (marker ``remediation``):
each test FAILS on the deliberate anti-pattern and PASSES once the recipe has been applied in the isolated
copy, on real PostgreSQL. The pipeline runs this module before the patch (observing the defect) and after it.

    uv run pytest -q -m remediation tests/test_remediation_targets.py
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.modules.ledger.antipatterns import commit_inside_begin
from tests.conftest import h, make_account

pytestmark = [pytest.mark.anyio, pytest.mark.remediation]


async def _balance(client, tenant_id: str, account_id: str) -> int:
    return (
        await client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account_id}", headers=h("alice"))
    ).json()["balance"]


async def test_commit_inside_begin_is_remediated(client, app_engine, acme):
    """After `remove_commit_inside_begin`: no InvalidRequestError and both deltas committed together."""
    account = await make_account(client, acme["id"], "alice")
    tenant, aid = uuid.UUID(acme["id"]), uuid.UUID(account["id"])
    start = await _balance(client, acme["id"], account["id"])
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session:
        await commit_inside_begin(session, tenant, aid)
    assert await _balance(client, acme["id"], account["id"]) == start + 2
