"""Test fixtures: one application per test with its own state."""

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from inventory_service.core.config import Settings
from inventory_service.main import create_app


@pytest.fixture
def app():
    return create_app(Settings(app_name="test", api_key=SecretStr("test-key")))


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}
