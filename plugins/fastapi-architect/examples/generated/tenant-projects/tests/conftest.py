"""Test fixtures: one application per test on a PostgreSQL database migrated by Alembic and truncated between tests; the
application connects as the restricted role (DATABASE_URL), migrations and truncation as the owner
(TENANT_PROJECTS_MIGRATIONS_DATABASE_URL). Identities: alice (tenant A, every role), bob (tenant A, member roles),
admin and limited (tenant A) when the policy declares them, carol (tenant B).
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

from tenant_projects.core.config import Settings
from tenant_projects.core.db import Base
from tenant_projects.main import create_app

TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
# name -> (tenant, roles); the credentials of each identity come from credentials(name)
IDENTITIES: dict[str, tuple[UUID, list[str]]] = {
    "alice": (TENANT_A, ["admin", "member"]),
    "bob": (TENANT_A, ["member"]),
    "admin": (TENANT_A, ["admin"]),
    "limited": (TENANT_A, []),
    "carol": (TENANT_B, ["admin", "member"]),
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
    env = dict(os.environ, TENANT_PROJECTS_DATABASE_URL=url, TENANT_PROJECTS_MIGRATIONS_DATABASE_URL=url)
    command = [sys.executable, "-m", "alembic", *args]
    return subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to an empty, disposable PostgreSQL database (application role)")
    return url


@pytest.fixture(scope="session")
def owner_url() -> str:
    url = os.environ.get("TENANT_PROJECTS_MIGRATIONS_DATABASE_URL")
    if not url:
        pytest.skip("set TENANT_PROJECTS_MIGRATIONS_DATABASE_URL to the owner of the same database")
    return url


@pytest.fixture(scope="session")
def migrated(owner_url: str):
    """Only an empty database is accepted: the suite migrates, truncates and downgrades it as the owner; the
    application never connects as the owner."""
    existing = run(table_names(owner_url))
    if existing:
        pytest.fail(f"TENANT_PROJECTS_MIGRATIONS_DATABASE_URL must be an empty database; found tables: {existing}")
    result = alembic(owner_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    yield owner_url
    alembic(owner_url, "downgrade", "base")


@pytest.fixture(autouse=True)
def clean_tables(migrated: str):
    yield
    run(truncate_all(migrated))


@pytest.fixture
def app(migrated: str, database_url: str):
    settings = Settings(app_name="test", api_keys=API_KEYS, database_url=database_url)
    return create_app(settings)


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
