"""API tests of the billing module through the HTTP boundary."""

import uuid

from tests.conftest import TENANT_B, credentials
from tests.test_orders import create_order

PAYLOAD_INVOICE = {"order_id": "11111111-1111-4111-8111-111111111111", "amount": "9.99", "issued_on": "2026-01-01"}


def invoice_url(tenant: uuid.UUID) -> str:
    return f"/api/v1/tenants/{tenant}/invoices"


def payload_for_invoice(client, headers, tenant) -> dict:
    payload = dict(PAYLOAD_INVOICE)
    payload["order_id"] = create_order(client, headers, tenant)["id"]
    return payload


def create_invoice(client, headers, tenant) -> dict:
    payload = payload_for_invoice(client, headers, tenant)
    response = client.post(invoice_url(tenant), json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_invoice(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["issued_on"] == PAYLOAD_INVOICE["issued_on"]


def test_create_invoice_rejects_invalid_payload(client, headers, tenant):
    url = invoice_url(tenant)
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_invoice_rejects_unknown_properties(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_create_invoice_with_unknown_order_id_is_a_conflict_and_leaves_no_row(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    payload["order_id"] = str(uuid.uuid4())
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert client.get(url, headers=headers).json() == []  # nothing was persisted


def test_create_invoice_cannot_reference_a_order_id_of_another_tenant(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    foreign = create_order(client, credentials("carol"), TENANT_B)
    payload["order_id"] = foreign["id"]
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.text  # composite foreign key (tenant_id, id)


def test_get_invoice_returns_the_created_one_and_404_for_unknown(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_get_invoice_with_a_malformed_id_is_422(client, headers, tenant):
    url = invoice_url(tenant)
    assert client.get(f"{url}/not-an-id", headers=headers).status_code == 422


def test_list_invoices_paginates(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    for _ in range(3):
        assert client.post(url, json=payload, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_invoice_routes_require_the_api_key(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401


def test_invoice_routes_refuse_another_tenant_for_the_same_principal(client, headers, tenant):
    payload = payload_for_invoice(client, headers, tenant)
    assert client.post(invoice_url(TENANT_B), json=payload, headers=headers).status_code == 403
    assert client.get(invoice_url(TENANT_B), headers=headers).status_code == 403


def test_invoices_of_one_tenant_are_invisible_to_another(client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    other = client.get(invoice_url(TENANT_B), headers=credentials("carol"))
    assert other.status_code == 200
    assert other.json() == []


def test_invoice_of_another_tenant_is_not_found_through_the_callers_tenant(client, headers, tenant):
    url = invoice_url(tenant)
    foreign = create_invoice(client, credentials("carol"), TENANT_B)
    foreign_url = f"{url}/{foreign['id']}"  # a valid id of another tenant, presented under the caller's tenant
    assert client.get(foreign_url, headers=headers).status_code == 404
    still_there = client.get(f"{invoice_url(TENANT_B)}/{foreign['id']}", headers=credentials("carol"))
    assert still_there.status_code == 200  # untouched


def test_create_invoice_notifies_the_notifications_port(app, client, headers, tenant):
    url = invoice_url(tenant)
    payload = payload_for_invoice(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    sent = app.state.ports["notifications"].sent
    assert sent and sent[-1]["event"] == "invoice.created"


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
