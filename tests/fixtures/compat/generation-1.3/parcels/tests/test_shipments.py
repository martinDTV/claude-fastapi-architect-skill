"""API tests of the shipments module through the HTTP boundary."""

import uuid

PAYLOAD_SHIPMENT = {"reference": "sample", "weight_kg": "9.99"}
SHIPMENT_URL = "/api/v1/shipments"


def payload_for_shipment(client, headers) -> dict:
    return dict(PAYLOAD_SHIPMENT)


def create_shipment(client, headers) -> dict:
    payload = payload_for_shipment(client, headers)
    response = client.post(SHIPMENT_URL, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_shipment(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["reference"] == PAYLOAD_SHIPMENT["reference"]


def test_create_shipment_rejects_invalid_payload(client, headers):
    url = SHIPMENT_URL
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_shipment_rejects_unknown_properties(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_get_shipment_returns_the_created_one_and_404_for_unknown(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    created = client.post(url, json=payload, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_get_shipment_with_a_malformed_id_is_422(client, headers):
    url = SHIPMENT_URL
    assert client.get(f"{url}/not-an-id", headers=headers).status_code == 422


def test_list_shipments_paginates(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    for _ in range(3):
        assert client.post(url, json=payload, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_update_shipment(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    created = client.post(url, json=payload, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"reference": "uuuuuu"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["reference"] == "uuuuuu"
    assert client.patch(f"{url}/{created['id']}", json={"unexpected": 1}, headers=headers).status_code == 422


def test_shipment_routes_require_the_api_key(client, headers):
    url = SHIPMENT_URL
    payload = payload_for_shipment(client, headers)
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
