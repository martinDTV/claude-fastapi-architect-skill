"""Test fixtures: one application per test on a PostgreSQL database migrated by Alembic and truncated between tests.
Identities: alice (tenant A, every role), bob (tenant A, member roles), admin and limited (tenant A) when the policy
declares them, carol (tenant B).
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from orders_platform.core.config import Settings
from orders_platform.core.db import Base
from orders_platform.main import create_app
from orders_platform.modules.billing.adapters.notifications import RecordingNotificationsAdapter
from orders_platform.modules.orders.adapters.payments import RecordingPaymentsAdapter

TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
# name -> (tenant, roles); the credentials of each identity come from credentials(name)
IDENTITIES: dict[str, tuple[UUID, list[str]]] = {
    "alice": (TENANT_A, []),
    "bob": (TENANT_A, []),
    "carol": (TENANT_B, []),
}
API_KEYS = {
    f"key-{name}": {"tenant_id": tenant, "subject": name, "roles": roles}
    for name, (tenant, roles) in IDENTITIES.items()
}


def credentials(identity: str) -> dict[str, str]:
    return {"X-API-Key": f"key-{identity}"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


async def table_names(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return sorted(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def truncate_all(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            if tables:
                await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


def alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, ORDERS_PLATFORM_DATABASE_URL=url)
    command = [sys.executable, "-m", "alembic", *args]
    return subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to an empty, disposable PostgreSQL database")
    return url


@pytest.fixture(scope="session")
def migrated(database_url: str):
    """Only an empty database is accepted: the suite migrates, truncates and downgrades it."""
    existing = run(table_names(database_url))
    if existing:
        pytest.fail(f"DATABASE_URL must point at an empty database; found tables: {existing}")
    result = alembic(database_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    yield database_url
    alembic(database_url, "downgrade", "base")


@pytest.fixture(autouse=True)
def clean_tables(migrated: str):
    yield
    run(truncate_all(migrated))


@pytest.fixture
def app(migrated: str):
    settings = Settings(app_name="test", api_keys=API_KEYS, database_url=migrated)
    ports = {"payments": RecordingPaymentsAdapter(), "notifications": RecordingNotificationsAdapter()}
    return create_app(settings, ports=ports)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return credentials("alice")


@pytest.fixture
def auth():
    """credentials(identity): the headers of alice, bob, admin, limited or carol."""
    return credentials


@pytest.fixture
def tenant() -> UUID:
    """The tenant of alice and bob (the default headers); carol belongs to TENANT_B."""
    return TENANT_A
