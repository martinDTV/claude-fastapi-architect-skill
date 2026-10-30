"""API tests of the catalog module through the HTTP boundary."""

import uuid


PAYLOAD_PRODUCT = {"sku": "sample", "name": "sample", "quantity": 1, "unit_price": "9.99", "notes": "sample"}
PRODUCT_URL = "/api/v1/products"


def test_create_product(client, headers):
    url = PRODUCT_URL
    response = client.post(url, json=PAYLOAD_PRODUCT, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["sku"] == PAYLOAD_PRODUCT["sku"]
    assert body["name"] == PAYLOAD_PRODUCT["name"]


def test_create_product_rejects_invalid_payload(client, headers):
    url = PRODUCT_URL
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_product_rejects_unknown_properties(client, headers):
    url = PRODUCT_URL
    response = client.post(url, json={**PAYLOAD_PRODUCT, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_get_product_returns_the_created_one_and_404_for_unknown(client, headers):
    url = PRODUCT_URL
    created = client.post(url, json=PAYLOAD_PRODUCT, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_list_products_paginates(client, headers):
    url = PRODUCT_URL
    for _ in range(3):
        assert client.post(url, json=PAYLOAD_PRODUCT, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_update_product(client, headers):
    url = PRODUCT_URL
    created = client.post(url, json=PAYLOAD_PRODUCT, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"sku": "uuuuuu"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["sku"] == "uuuuuu"
    assert client.patch(f"{url}/{created['id']}", json={"unexpected": 1}, headers=headers).status_code == 422


def test_delete_product(client, headers):
    url = PRODUCT_URL
    created = client.post(url, json=PAYLOAD_PRODUCT, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=headers).status_code == 204
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 404


def test_product_routes_require_the_api_key(client):
    url = PRODUCT_URL
    assert client.post(url, json=PAYLOAD_PRODUCT).status_code == 401
    assert client.post(url, json=PAYLOAD_PRODUCT, headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
