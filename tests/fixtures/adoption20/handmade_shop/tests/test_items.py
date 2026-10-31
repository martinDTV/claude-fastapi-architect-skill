import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture(autouse=True)
def _clean():
    storage.reset()
    yield
    storage.reset()


@pytest.fixture
def client():
    return TestClient(app)


def test_create_and_get_item(client):
    created = client.post("/api/items", json={"name": "lamp", "price": 12.5})
    assert created.status_code == 201
    item = created.json()
    assert client.get(f"/api/items/{item['id']}").json() == item
    assert client.get("/api/items").json() == [item]


def test_missing_item_is_404(client):
    assert client.get("/api/items/999").status_code == 404


def test_users_are_separate(client):
    assert client.post("/api/users", json={"email": "a@example.org"}).status_code == 201
    assert client.get("/api/users/1").json()["email"] == "a@example.org"
    assert client.get("/api/items").json() == []
