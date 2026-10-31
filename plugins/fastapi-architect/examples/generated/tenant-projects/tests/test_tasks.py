"""API tests of the tasks module through the HTTP boundary: every declared operation, the role each one needs, ownership
across members and admins, and tenant isolation.
"""

import uuid

from tests.conftest import TENANT_B, credentials
from tests.test_projects import create_project

PAYLOAD_TASK = {"project_id": "11111111-1111-4111-8111-111111111111", "title": "sample", "done": True}


def task_url(tenant: uuid.UUID) -> str:
    return f"/api/v1/tenants/{tenant}/tasks"


def payload_for_task(client, headers, tenant) -> dict:
    payload = dict(PAYLOAD_TASK)
    payload["project_id"] = create_project(client, headers, tenant)["id"]
    return payload


def create_task(client, headers, tenant) -> dict:
    payload = payload_for_task(client, headers, tenant)
    response = client.post(task_url(tenant), json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_task(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["title"] == PAYLOAD_TASK["title"]
    assert body["done"] == PAYLOAD_TASK["done"]


def test_create_task_rejects_invalid_payload(client, headers, tenant):
    url = task_url(tenant)
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 422


def test_create_task_rejects_unknown_properties(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)
    assert response.status_code == 422


def test_create_task_with_unknown_project_id_is_a_conflict_and_leaves_no_row(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    payload["project_id"] = str(uuid.uuid4())
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert client.get(url, headers=headers).json() == []  # nothing was persisted


def test_create_task_cannot_reference_a_project_id_of_another_tenant(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    foreign = create_project(client, credentials("carol"), TENANT_B)
    payload["project_id"] = foreign["id"]
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.text  # composite foreign key (tenant_id, id)


def test_get_task_returns_the_created_one_and_404_for_unknown(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    found = client.get(f"{url}/{created['id']}", headers=headers)
    assert found.status_code == 200
    assert found.json()["id"] == created["id"]
    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["status"] == 404


def test_get_task_with_a_malformed_id_is_422(client, headers, tenant):
    url = task_url(tenant)
    assert client.get(f"{url}/not-an-id", headers=headers).status_code == 422


def test_list_tasks_paginates(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    for _ in range(3):
        assert client.post(url, json=payload, headers=headers).status_code == 201
    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422


def test_update_task(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"title": "uuuuuu"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "uuuuuu"
    assert client.patch(f"{url}/{created['id']}", json={"unexpected": 1}, headers=headers).status_code == 422


def test_delete_task(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=headers).status_code == 204
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 404


def test_task_routes_require_the_api_key(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401


def test_task_routes_refuse_another_tenant_for_the_same_principal(client, headers, tenant):
    payload = payload_for_task(client, headers, tenant)
    assert client.post(task_url(TENANT_B), json=payload, headers=headers).status_code == 403
    assert client.get(task_url(TENANT_B), headers=headers).status_code == 403


def test_tasks_of_one_tenant_are_invisible_to_another(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    other = client.get(task_url(TENANT_B), headers=credentials("carol"))
    assert other.status_code == 200
    assert other.json() == []


def test_task_of_another_tenant_is_not_found_through_the_callers_tenant(client, headers, tenant):
    url = task_url(tenant)
    foreign = create_task(client, credentials("carol"), TENANT_B)
    foreign_url = f"{url}/{foreign['id']}"  # a valid id of another tenant, presented under the caller's tenant
    assert client.get(foreign_url, headers=headers).status_code == 404
    assert client.patch(foreign_url, json={"title": "uuuuuu"}, headers=headers).status_code == 404
    assert client.delete(foreign_url, headers=headers).status_code == 404
    still_there = client.get(f"{task_url(TENANT_B)}/{foreign['id']}", headers=credentials("carol"))
    assert still_there.status_code == 200  # untouched


def test_delete_task_needs_a_permitted_role(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.delete(f"{url}/{created['id']}", headers=credentials("limited")).status_code == 403
    assert client.get(f"{url}/{created['id']}", headers=headers).status_code == 200  # untouched


def test_task_records_the_creating_principal_as_owner_and_never_takes_it_from_the_client(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    assert client.post(url, json={**payload, "owner_id": "mallory"}, headers=headers).status_code == 422
    created = client.post(url, json=payload, headers=headers).json()
    assert created["owner_id"] == "alice"


def test_another_member_reads_task_of_a_colleague(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    assert client.get(f"{url}/{created['id']}", headers=credentials("bob")).status_code == 200  # visibility: tenant


def test_list_tasks_is_shared_within_the_tenant(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    assert client.post(url, json=payload, headers=headers).status_code == 201
    assert len(client.get(url, headers=credentials("bob")).json()) == 1


def test_another_member_cannot_update_task_of_a_colleague(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    denied = client.patch(f"{url}/{created['id']}", json={"title": "uuuuuu"}, headers=credentials("bob"))
    assert denied.status_code == 403, denied.text
    assert client.get(f"{url}/{created['id']}", headers=headers).json()["title"] == created["title"]  # unchanged


def test_an_admin_can_update_task_of_another_member(client, headers, tenant):
    url = task_url(tenant)
    payload = payload_for_task(client, headers, tenant)
    created = client.post(url, json=payload, headers=headers).json()
    updated = client.patch(f"{url}/{created['id']}", json={"title": "uuuuuu"}, headers=credentials("admin"))
    assert updated.status_code == 200, updated.text


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
