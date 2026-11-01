"""Test fixtures: one application per test on a PostgreSQL database migrated by Alembic and truncated between tests."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from parcels.core.config import Settings
from parcels.core.db import Base
from parcels.main import create_app

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
    env = dict(os.environ, PARCELS_DATABASE_URL=url)
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
    existing = [t for t in run(table_names(database_url)) if t != "alembic_version"]  # a downgrade leaves it empty
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
    settings = Settings(app_name="test", api_key=SecretStr("test-key"), database_url=migrated)
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}
