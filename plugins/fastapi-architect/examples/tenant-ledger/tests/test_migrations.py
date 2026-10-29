"""Migrations on an empty database and from the previous revision, with the schema in
sync with the models (alembic check)."""

import uuid

import pytest

from tests.conftest import run_alembic

pytestmark = pytest.mark.anyio


@pytest.fixture
def fresh_db(pg):
    psql = pg["psql_owner"]
    name = f"ledger_mig_{uuid.uuid4().hex[:8]}"
    psql(f"CREATE DATABASE {name}", db="postgres")
    url = pg["owner_url"].rsplit("/", 1)[0] + "/" + name
    try:
        yield name, url, psql
    finally:
        psql(f"DROP DATABASE {name} WITH (FORCE)", db="postgres")


def test_upgrade_from_empty_then_check(fresh_db):
    name, url, psql = fresh_db
    up = run_alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    tables = psql(
        "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'",
        db=name,
    )
    assert "accounts" in tables and "alembic_version" in tables and "idempotency_keys" in tables
    policies = psql("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'", db=name)
    assert policies == "3"
    rls = psql(
        "SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE relname IN ('accounts','entries','idempotency_keys')",
        db=name,
    )
    assert rls == "t"
    check = run_alembic(url, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_upgrade_from_the_previous_revision(fresh_db):
    name, url, psql = fresh_db
    assert run_alembic(url, "upgrade", "0001").returncode == 0
    assert psql("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'", db=name) == "0"
    # the previous release's schema, with rows in it
    psql(f"INSERT INTO tenants (id, slug, name) VALUES ('{uuid.uuid4()}', 'legacy', 'Legacy')", db=name)
    up = run_alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert psql("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'", db=name) == "3"
    assert psql("SELECT count(*) FROM tenants", db=name) == "1"
    down = run_alembic(url, "downgrade", "0001")
    assert down.returncode == 0, down.stderr
    assert psql("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'", db=name) == "0"
    assert psql("SELECT version_num FROM alembic_version", db=name) == "0001"
