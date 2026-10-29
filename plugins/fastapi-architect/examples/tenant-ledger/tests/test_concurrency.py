"""Concurrency without lost updates: atomic balance changes and optimistic renames."""

import asyncio

import pytest

from tests.conftest import h, make_account

pytestmark = pytest.mark.anyio


async def test_fifty_concurrent_deposits_lose_nothing(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/deposits"
    responses = await asyncio.gather(
        *[client.post(url, json={"amount": 10}, headers=h("bob")) for _ in range(50)]
    )
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses if r.status_code != 201][
        :3
    ]
    read = await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice"))
    assert read.json()["balance"] == 500
    assert read.json()["version"] == 51
    entries = await client.get(
        f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}/entries", headers=h("alice")
    )
    assert len(entries.json()) == 50


async def test_overdraft_is_rejected_atomically_under_concurrency(client, acme):
    account = await make_account(client, acme["id"], "alice")
    base = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}"
    assert (
        await client.post(f"{base}/deposits", json={"amount": 100}, headers=h("alice"))
    ).status_code == 201
    responses = await asyncio.gather(
        *[client.post(f"{base}/withdrawals", json={"amount": 30}, headers=h("alice")) for _ in range(5)]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 201, 201, 409, 409], statuses
    read = await client.get(base, headers=h("alice"))
    assert read.json()["balance"] == 10


async def test_if_match_protects_renames(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}"
    etag = account["etag"]
    missing = await client.patch(url, json={"name": "savings"}, headers=h("alice"))
    assert missing.status_code == 428
    ok = await client.patch(url, json={"name": "savings"}, headers={**h("alice"), "If-Match": etag})
    assert ok.status_code == 200 and ok.headers["ETag"] == '"2"'
    stale = await client.patch(url, json={"name": "checking"}, headers={**h("bob"), "If-Match": etag})
    assert stale.status_code == 412
    assert '"2"' in stale.json()["detail"]
    read = await client.get(url, headers=h("alice"))
    assert read.json()["name"] == "savings"


async def test_two_concurrent_renames_with_the_same_etag_exactly_one_wins(client, acme):
    account = await make_account(client, acme["id"], "alice")
    url = f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}"
    a, b = await asyncio.gather(
        client.patch(url, json={"name": "a"}, headers={**h("alice"), "If-Match": account["etag"]}),
        client.patch(url, json={"name": "b"}, headers={**h("bob"), "If-Match": account["etag"]}),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 412]
