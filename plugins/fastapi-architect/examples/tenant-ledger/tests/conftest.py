"""Test infrastructure: a throwaway PostgreSQL cluster (initdb/pg_ctl) unless
LEDGER_TEST_OWNER_URL points at an isolated database you own, real migrations through
Alembic, the application wired to the non-owner role, and an in-process HTTP client.

Never point LEDGER_TEST_OWNER_URL at a shared or production database: the suite creates
databases and roles and runs migrations up and down.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ledger.core.config import Settings
from ledger.main import create_app

ROOT = Path(__file__).resolve().parents[1]
APP_ROLE = "ledger_app"
OWNER_ROLE = "ledger_owner"
DB_NAME = "ledger_test"


def find_pg(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for prefix in (
        "/opt/homebrew/opt/postgresql@16/bin",
        "/opt/homebrew/opt/postgresql@17/bin",
        "/opt/homebrew/opt/postgresql@15/bin",
        "/usr/lib/postgresql/16/bin",
        "/usr/lib/postgresql/17/bin",
        "/usr/local/pgsql/bin",
    ):
        candidate = os.path.join(prefix, binary)
        if os.path.exists(candidate):
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def pg() -> Iterator[dict[str, str]]:
    """Connection details for an isolated PostgreSQL: {owner_url, app_url, psql: callable}."""
    external = os.environ.get("LEDGER_TEST_OWNER_URL")
    if external:
        app_url = os.environ.get("LEDGER_TEST_APP_URL")
        if not app_url:
            pytest.skip("LEDGER_TEST_APP_URL is required together with LEDGER_TEST_OWNER_URL")
        yield {
            "owner_url": external,
            "app_url": app_url,
            "psql_owner": os.environ.get("LEDGER_TEST_PSQL", ""),
        }
        return
    initdb, pg_ctl, psql = find_pg("initdb"), find_pg("pg_ctl"), find_pg("psql")
    if not (initdb and pg_ctl and psql):
        pytest.skip(
            "PostgreSQL binaries (initdb, pg_ctl, psql) not found; set LEDGER_TEST_OWNER_URL to use your own isolated database"
        )
    tmp = tempfile.mkdtemp(prefix="ledger-pg-")
    data, sockets = os.path.join(tmp, "data"), os.path.join(tmp, "sock")
    os.makedirs(sockets)
    port = free_port()
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    subprocess.run(
        [initdb, "-D", data, "-U", OWNER_ROLE, "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            data,
            "-l",
            os.path.join(tmp, "pg.log"),
            "-w",
            "-t",
            "30",
            "-o",
            f"-p {port} -k {sockets} -h 127.0.0.1 -c fsync=off -c synchronous_commit=off -c max_connections=100",
            "start",
        ],
        check=True,
        capture_output=True,
        env=env,
    )

    def run_psql(sql: str, db: str = DB_NAME, user: str = OWNER_ROLE) -> str:
        return subprocess.run(
            [
                psql,
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                db,
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    run_psql(f"CREATE DATABASE {DB_NAME}", db="postgres")
    details = {
        "owner_url": f"postgresql+asyncpg://{OWNER_ROLE}@127.0.0.1:{port}/{DB_NAME}",
        "app_url": f"postgresql+asyncpg://{APP_ROLE}@127.0.0.1:{port}/{DB_NAME}",
        "port": str(port),
    }
    details["psql_owner"] = run_psql  # type: ignore[assignment]
    try:
        yield details
    finally:
        subprocess.run([pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"], capture_output=True, env=env)
        shutil.rmtree(tmp, ignore_errors=True)


def run_alembic(owner_url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, LEDGER_MIGRATIONS_DATABASE_URL=owner_url, LEDGER_APP_ROLE=APP_ROLE)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


@pytest.fixture(scope="session")
def migrated(pg: dict[str, str]) -> dict[str, str]:
    result = run_alembic(pg["owner_url"], "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return pg


@pytest.fixture(scope="session")
def settings(migrated: dict[str, str]) -> Settings:
    return Settings(
        database_url=migrated["app_url"], migrations_database_url=migrated["owner_url"], _env_file=None
    )


@pytest.fixture(scope="session")
def seeded_users(migrated: dict[str, str]) -> list[str]:
    psql = migrated["psql_owner"]
    for name in ("alice", "bob", "carol"):
        psql(
            f"INSERT INTO users (id, username) VALUES ('{uuid.uuid4()}', '{name}') ON CONFLICT (username) DO NOTHING"
        )
    return ["alice", "bob", "carol"]


@pytest.fixture
async def app(settings: Settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def owner_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.migrations_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def app_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def h(user: str) -> dict[str, str]:
    return {"X-Demo-User": user}


async def make_tenant(client: httpx.AsyncClient, owner: str, members: tuple[str, ...] = ()) -> dict:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/v1/tenants", json={"slug": slug, "name": slug.upper()}, headers=h(owner)
    )
    assert created.status_code == 201, created.text
    tenant = created.json()
    for member in members:
        added = await client.post(
            f"/api/v1/tenants/{tenant['id']}/members",
            json={"username": member, "role": "member"},
            headers=h(owner),
        )
        assert added.status_code == 201, added.text
    return tenant


async def make_account(client: httpx.AsyncClient, tenant_id: str, user: str, name: str = "main") -> dict:
    created = await client.post(f"/api/v1/tenants/{tenant_id}/accounts", json={"name": name}, headers=h(user))
    assert created.status_code == 201, created.text
    body = created.json()
    body["etag"] = created.headers["ETag"]
    return body


@pytest.fixture
async def acme(client: httpx.AsyncClient, seeded_users) -> dict:
    return await make_tenant(client, "alice", members=("bob",))


@pytest.fixture
async def globex(client: httpx.AsyncClient, seeded_users) -> dict:
    return await make_tenant(client, "carol")
