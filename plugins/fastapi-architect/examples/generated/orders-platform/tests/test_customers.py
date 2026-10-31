"""API tests of the customers module through the HTTP boundary."""

import uuid

OTHER_TENANT = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

PAYLOAD_CUSTOMER = {"email": "sample", "display_name": "sample", "active": True}


def customer_url(tenant: uuid.UUID) -> str:
    return f"/api/v1/tenants/{tenant}/customers"


def payload_for_customer(client, headers, tenant) -> dict:
    return dict(PAYLOAD_CUSTOMER)


def create_customer(client, headers, tenant) -> dict:
    payload = payload_for_customer(client, headers, tenant)
    response = client.post(customer_url(tenant), json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_customer(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["email"] == PAYLOAD_CUSTOMER["email"]
    assert body["display_name"] == PAYLOAD_CUSTOMER["display_name"]


def test_create_customer_rejects_invalid_payload(client, headers, tenant):
    url = customer_url(tenant)
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_customer_rejects_unknown_properties(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_get_customer_returns_the_created_one_and_404_for_unknown(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_list_customers_paginates(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    for _ in range(3):
        assert client.post(url, json=payload, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_update_customer(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"email": "uuuuuu"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "uuuuuu"
    assert client.patch(f"{url}/{created['id']}", json={"unexpected": 1}, headers=headers).status_code == 422


def test_delete_customer(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=headers).status_code == 204
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 404


def test_customer_routes_require_the_api_key(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401


def test_customer_routes_refuse_another_tenant_for_the_same_key(client, headers, tenant):
    payload = payload_for_customer(client, headers, tenant)
    assert client.post(customer_url(OTHER_TENANT), json=payload, headers=headers).status_code == 403
    assert client.get(customer_url(OTHER_TENANT), headers=headers).status_code == 403


def test_customers_of_one_tenant_are_invisible_to_another(client, headers, tenant):
    url = customer_url(tenant)
    payload = payload_for_customer(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    other = client.get(customer_url(OTHER_TENANT), headers={"X-API-Key": "key-b"})
    assert other.status_code == 200
    assert other.json() == []


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
