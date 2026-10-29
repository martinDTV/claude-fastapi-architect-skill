"""Demo authentication, resource authorization and tenant isolation through the API."""

import pytest

from tests.conftest import h, make_account

pytestmark = pytest.mark.anyio


async def test_member_can_create_and_read_an_account(client, acme):
    account = await make_account(client, acme["id"], "bob")  # member: allowed
    read = await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice"))
    assert read.status_code == 200
    assert read.json()["name"] == "main"
    assert read.headers["ETag"] == '"1"'


async def test_only_owners_can_delete_accounts(client, acme):
    account = await make_account(client, acme["id"], "alice")
    denied = await client.delete(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("bob"))
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/problem+json")
    assert denied.json()["title"] == "Forbidden"
    allowed = await client.delete(
        f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice")
    )
    assert allowed.status_code == 204
    gone = await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{account['id']}", headers=h("alice"))
    assert gone.status_code == 404


async def test_missing_or_unknown_demo_identity_is_rejected(client, acme):
    assert (await client.get("/api/v1/tenants")).status_code == 401
    assert (await client.get("/api/v1/tenants", headers=h("mallory"))).status_code == 401
    body = (await client.get("/api/v1/tenants")).json()
    assert body["status"] == 401 and "demo" in body["detail"].lower()


async def test_cross_tenant_access_is_denied_for_every_verb(client, acme, globex):
    account = await make_account(client, acme["id"], "alice")
    base = f"/api/v1/tenants/{acme['id']}/accounts"
    # carol is not a member of acme: 404 everywhere, never 403 (no enumeration)
    assert (await client.get(base, headers=h("carol"))).status_code == 404
    assert (await client.get(f"{base}/{account['id']}", headers=h("carol"))).status_code == 404
    assert (
        await client.post(f"{base}/{account['id']}/deposits", json={"amount": 5}, headers=h("carol"))
    ).status_code == 404
    assert (
        await client.patch(
            f"{base}/{account['id']}", json={"name": "x"}, headers={**h("carol"), "If-Match": '"1"'}
        )
    ).status_code == 404
    assert (await client.delete(f"{base}/{account['id']}", headers=h("carol"))).status_code == 404
    # and acme's data did not change
    read = await client.get(f"{base}/{account['id']}", headers=h("alice"))
    assert read.json()["balance"] == 0 and read.json()["name"] == "main"


async def test_an_account_id_from_another_tenant_is_invisible_even_to_a_member(client, acme, globex):
    foreign = await make_account(client, globex["id"], "carol")
    # alice is a member of acme and asks acme for globex's account id: the row is out of scope
    assert (
        await client.get(f"/api/v1/tenants/{acme['id']}/accounts/{foreign['id']}", headers=h("alice"))
    ).status_code == 404


async def test_validation_errors_are_problem_details(client, acme):
    bad = await client.post(f"/api/v1/tenants/{acme['id']}/accounts", json={"name": ""}, headers=h("alice"))
    assert bad.status_code == 422
    assert bad.headers["content-type"].startswith("application/problem+json")
    assert "name" in bad.json()["detail"]
