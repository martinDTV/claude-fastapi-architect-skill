"""A lost response must not apply a movement twice."""

import asyncio
import uuid

import pytest

from tests.conftest import h, make_account

pytestmark = pytest.mark.anyio


async def test_replaying_the_same_idempotency_key_returns_the_first_result(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits"
    key = uuid.uuid4().hex
    first = await client.post(url, json={"amount": 70}, headers={**h("alice"), "Idempotency-Key": key})
    replay = await client.post(url, json={"amount": 70}, headers={**h("alice"), "Idempotency-Key": key})
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    read = await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice"))
    assert read.json()["balance"] == 70


async def test_concurrent_duplicates_with_one_key_apply_exactly_once(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits"
    key = uuid.uuid4().hex
    responses = await asyncio.gather(
        *[
            client.post(url, json={"amount": 5}, headers={**h("bob"), "Idempotency-Key": key})
            for _ in range(10)
        ]
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["entry_id"] for r in responses}) == 1
    read = await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice"))
    assert read.json()["balance"] == 5


async def test_keys_are_scoped_per_tenant(client, acme, globex):
    a = await make_account(client, acme["id"], "alice")
    g = await make_account(client, globex["id"], "carol")
    key = "shared-key"
    ra = await client.post(
        f"/api/v1/tenants/{acme['id']}/accounts/{a['id']}/deposits",
        json={"amount": 1},
        headers={**h("alice"), "Idempotency-Key": key},
    )
    rg = await client.post(
        f"/api/v1/tenants/{globex['id']}/accounts/{g['id']}/deposits",
        json={"amount": 2},
        headers={**h("carol"), "Idempotency-Key": key},
    )
    assert ra.status_code == rg.status_code == 201 and ra.json()["entry_id"] != rg.json()["entry_id"]
