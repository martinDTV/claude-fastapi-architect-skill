"""API tests of the projects module through the HTTP boundary: every declared operation, the role each one needs,
ownership across members and admins, and tenant isolation.
"""

import uuid

from tests.conftest import TENANT_B, credentials

PAYLOAD_PROJECT = {"name": "sample", "budget": "9.99", "starts_on": "2026-01-01"}


def project_url(tenant: uuid.UUID) -> str:
    return f"/api/v1/tenants/{tenant}/projects"


def payload_for_project(client, headers, tenant) -> dict:
    return dict(PAYLOAD_PROJECT)


def create_project(client, headers, tenant) -> dict:
    payload = payload_for_project(client, headers, tenant)
    response = client.post(project_url(tenant), json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_project(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["name"] == PAYLOAD_PROJECT["name"]
    assert body["starts_on"] == PAYLOAD_PROJECT["starts_on"]


def test_create_project_rejects_invalid_payload(client, headers, tenant):
    url = project_url(tenant)
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_project_rejects_unknown_properties(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_get_project_returns_the_created_one_and_404_for_unknown(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_get_project_with_a_malformed_id_is_422(client, headers, tenant):
    url = project_url(tenant)
    assert client.get(f"{url}/not-an-id", headers=headers).status_code == 422


def test_list_projects_paginates(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    for _ in range(3):
        assert client.post(url, json=payload, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_update_project(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"name": "uuuuuu"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "uuuuuu"
    assert client.patch(f"{url}/{created['id']}", json={"unexpected": 1}, headers=headers).status_code == 422


def test_delete_project(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=headers).status_code == 204
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 404


def test_project_routes_require_the_api_key(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401


def test_project_routes_refuse_another_tenant_for_the_same_principal(client, headers, tenant):
    payload = payload_for_project(client, headers, tenant)
    assert client.post(project_url(TENANT_B), json=payload, headers=headers).status_code == 403
    assert client.get(project_url(TENANT_B), headers=headers).status_code == 403


def test_projects_of_one_tenant_are_invisible_to_another(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    other = client.get(project_url(TENANT_B), headers=credentials("carol"))
    assert other.status_code == 200
    assert other.json() == []


def test_project_of_another_tenant_is_not_found_through_the_callers_tenant(client, headers, tenant):
    url = project_url(tenant)
    foreign = create_project(client, credentials("carol"), TENANT_B)
    foreign_url = f"{url}/{foreign['id']}"  # a valid id of another tenant, presented under the caller's tenant
    assert client.get(foreign_url, headers=headers).status_code == 404
    assert client.patch(foreign_url, json={"name": "uuuuuu"}, headers=headers).status_code == 404
    assert client.delete(foreign_url, headers=headers).status_code == 404
    still_there = client.get(f"{project_url(TENANT_B)}/{foreign['id']}", headers=credentials("carol"))
    assert still_there.status_code == 200  # untouched


def test_delete_project_needs_a_permitted_role(client, headers, tenant):
    url = project_url(tenant)
    payload = payload_for_project(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=credentials("limited")).status_code == 403
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 200  # untouched


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
