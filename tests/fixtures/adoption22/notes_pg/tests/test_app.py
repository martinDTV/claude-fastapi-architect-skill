"""The project's own tests: no database needed (the application and its routes)."""
from fastapi.testclient import TestClient

from app.main import app


def test_health():
    assert TestClient(app).get("/health").json() == {"ok": True}


def test_notes_routes_are_mounted():
    paths = set(app.openapi()["paths"])
    assert "/api/notes" in paths
    assert "/api/notes/{note_id}" in paths
