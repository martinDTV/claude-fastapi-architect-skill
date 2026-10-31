"""Row-level security, demonstrated through the application role and raw SQL (no application filter involved): policies
exist and are forced, no context means no rows, the context is bound to the transaction and never survives commit,
rollback or the return of a pooled connection, another tenant's rows cannot be read, written, updated or deleted, the
application role cannot lift the protection, and concurrent requests of two tenants never see each other.
"""

import socket
import uuid

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tenant_projects.core.config import Settings
from tenant_projects.main import create_app
from tests.conftest import TENANT_A, TENANT_B, credentials, run
from tests.test_projects import PAYLOAD_PROJECT, create_project

TABLE = "projects"
TABLES = ["projects", "tasks"]
CONTEXT_KEY = "app.tenant_id"
TENANT_COLUMN = "tenant_id"
INSERT_ROW = (
    "INSERT INTO projects (id, tenant_id, name, budget, starts_on) VALUES (gen_random_uuid(), "
    "CAST(:tenant AS text)::uuid, :name, CAST(:budget AS text)::numeric, CAST(:starts_on AS text)::date) "
)
SET_CONTEXT = "SELECT set_config(:key, :tenant, true)"
READ_CONTEXT = "SELECT current_setting(:key, true)"


def url_for(tenant: uuid.UUID) -> str:
    return f"/api/v1/tenants/{tenant}/projects"


def row_of(tenant: uuid.UUID) -> dict:
    return {"tenant": str(tenant), **PAYLOAD_PROJECT}


async def sql(url: str, statement: str, params: dict | None = None, *, tenant: object = None, commit: bool = False):
    """One statement on a fresh engine; with ``tenant`` the context is set first, in the same transaction."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            if tenant is not None:
                await connection.execute(text(SET_CONTEXT), {"key": CONTEXT_KEY, "tenant": str(tenant)})
            result = await connection.execute(text(statement), params or {})
            rows = result.all() if result.returns_rows else result.rowcount
            if commit:
                await connection.commit()
            else:
                await connection.rollback()
            return rows
    finally:
        await engine.dispose()


def seed(client) -> tuple[dict, dict]:
    """One row of tenant A (alice) and one of tenant B (carol), created through the API."""
    of_a = create_project(client, credentials("alice"), TENANT_A)
    of_b = create_project(client, credentials("carol"), TENANT_B)
    return of_a, of_b


def test_every_tenant_table_has_rls_enabled_forced_and_a_policy(migrated):
    query = "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables) ORDER BY 1"
    rows = run(sql(migrated, query, {"tables": TABLES}))
    assert [r[0] for r in rows] == TABLES
    assert all(r[1] and r[2] for r in rows), rows
    query = "SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE tablename = ANY(:tables)"
    policies = run(sql(migrated, query, {"tables": TABLES}))
    assert sorted(p[0] for p in policies) == TABLES
    for _table, name, qual, with_check in policies:
        assert name.endswith("_tenant_isolation")
        assert CONTEXT_KEY in qual and CONTEXT_KEY in with_check


def test_the_application_role_is_restricted(database_url):
    query = "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
    row = run(sql(database_url, query))[0]
    assert tuple(row) == (False, False, False, False), row


def test_without_tenant_context_no_row_is_visible(client, migrated, database_url):
    seed(client)
    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == 2  # the owner sees both
    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}"))[0][0] == 0
    assert run(sql(database_url, f"SELECT * FROM {TABLE}")) == []


def test_without_tenant_context_an_insert_is_rejected(database_url):
    with pytest.raises(DBAPIError, match="row-level security"):
        run(sql(database_url, INSERT_ROW, row_of(TENANT_A), commit=True))


def test_with_tenant_context_only_that_tenants_rows_are_visible(client, database_url):
    seed(client)
    rows = run(sql(database_url, f"SELECT {TENANT_COLUMN} FROM {TABLE}", tenant=TENANT_A))
    assert [str(r[0]) for r in rows] == [str(TENANT_A)]
    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_B))[0][0] == 1


def test_with_tenant_context_a_row_of_another_tenant_cannot_be_written(client, database_url):
    _created, other = seed(client)
    with pytest.raises(DBAPIError, match="row-level security"):
        run(sql(database_url, INSERT_ROW, row_of(TENANT_B), tenant=TENANT_A, commit=True))
    with pytest.raises(DBAPIError, match="row-level security"):
        move = f"UPDATE {TABLE} SET {TENANT_COLUMN} = CAST(:b AS text)::uuid"
        move += f" WHERE {TENANT_COLUMN} = CAST(:a AS text)::uuid"
        tenants = {"a": str(TENANT_A), "b": str(TENANT_B)}
        run(sql(database_url, move, tenants, tenant=TENANT_A, commit=True))
    touch = f"UPDATE {TABLE} SET created_at = now() WHERE id = CAST(:id AS text)::uuid"
    assert run(sql(database_url, touch, {"id": other["id"]}, tenant=TENANT_A, commit=True)) == 0
    drop = f"DELETE FROM {TABLE} WHERE id = CAST(:id AS text)::uuid"
    assert run(sql(database_url, drop, {"id": other["id"]}, tenant=TENANT_A, commit=True)) == 0
    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_B))[0][0] == 1  # untouched


def test_tenant_context_does_not_survive_commit_rollback_or_the_pool(database_url):
    async def scenario() -> list:
        engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
        seen = []
        try:
            for end in ("commit", "rollback"):
                async with engine.connect() as connection:
                    await connection.execute(text(SET_CONTEXT), {"key": CONTEXT_KEY, "tenant": str(TENANT_A)})
                    inside = (await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar()
                    assert inside == str(TENANT_A)
                    await (connection.commit() if end == "commit" else connection.rollback())
                    seen.append((await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar())
                async with engine.connect() as connection:  # the same pooled connection, next checkout
                    seen.append((await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar())
        finally:
            await engine.dispose()
        return seen

    seen = run(scenario())
    assert len(seen) == 4
    assert all(value in (None, "") for value in seen), seen


def test_rollback_leaves_no_row(client, migrated, database_url):
    seed(client)
    before = run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0]
    assert run(sql(database_url, INSERT_ROW, row_of(TENANT_A), tenant=TENANT_A, commit=False)) == 1  # then rolled back
    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == before
    assert run(sql(database_url, INSERT_ROW, row_of(TENANT_A), tenant=TENANT_A, commit=True)) == 1
    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == before + 1


def test_an_invalid_or_empty_tenant_context_fails_closed(client, database_url):
    seed(client)
    with pytest.raises(DBAPIError, match="invalid input syntax for type uuid"):
        run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant="not-a-tenant"))
    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=""))[0][0] == 0


def test_the_application_role_cannot_lift_the_protection(database_url):
    for statement, reason in (
        (f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY", "must be owner"),
        (f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY", "must be owner"),
        (f"DROP POLICY {TABLE}_tenant_isolation ON {TABLE}", "must be owner"),
        (f"TRUNCATE TABLE {TABLE}", "permission denied"),
        ("ALTER ROLE current_user BYPASSRLS", "permission denied|must be superuser"),
    ):
        with pytest.raises(DBAPIError, match=reason):
            run(sql(database_url, statement, commit=True))


def test_the_context_is_a_setting_that_any_statement_run_as_the_application_role_can_change(client, database_url):
    """The boundary of the mechanism: the policies read a transaction setting, and the application role may set it. A
    statement executed as that role (application code, or an injection that reaches statement level) can switch tenant;
    a bound parameter cannot. Row-level security protects against missing filters, not against arbitrary SQL."""
    seed(client)
    switch = f"SELECT set_config(:key, :other, true), (SELECT count(*) FROM {TABLE})"
    rows = run(sql(database_url, switch, {"key": CONTEXT_KEY, "other": str(TENANT_B)}, tenant=TENANT_A))
    assert rows[0][1] == 1  # after the switch the same transaction sees tenant B's row: the limit, demonstrated
    injected = f"'; SELECT set_config('{CONTEXT_KEY}', '{TENANT_B}', true); --"
    probe = f"SELECT count(*) FROM {TABLE} WHERE CAST(id AS text) = :value"
    # a bound parameter is data, not a statement: nothing is switched and the context stays on tenant A
    assert run(sql(database_url, probe, {"value": injected}, tenant=TENANT_A))[0][0] == 0
    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_A))[0][0] == 1


def test_the_application_role_has_row_privileges_only(database_url):
    for statement, reason in (
        ("SELECT rolpassword FROM pg_authid LIMIT 1", "permission denied"),
        ("SELECT * FROM alembic_version", "permission denied"),
        ("CREATE TABLE smuggled (id int)", "permission denied"),
        (f"CREATE POLICY open_door ON {TABLE} USING (true)", "must be owner"),
        (f"ALTER TABLE {TABLE} OWNER TO current_user", "must be owner|permission denied"),
    ):
        with pytest.raises(DBAPIError, match=reason):
            run(sql(database_url, statement, commit=True))


def test_concurrent_requests_of_two_tenants_never_see_each_other(app):
    async def scenario() -> list[tuple[str, int, list]]:
        results: list[tuple[str, int, list]] = []
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                for identity, tenant, count in (("alice", TENANT_A, 3), ("carol", TENANT_B, 2)):
                    for _ in range(count):
                        response = await http.post(url_for(tenant), json=PAYLOAD_PROJECT, headers=credentials(identity))
                        assert response.status_code == 201, response.text

                async def one(identity: str, tenant: uuid.UUID) -> None:
                    response = await http.get(url_for(tenant), headers=credentials(identity))
                    results.append((str(tenant), response.status_code, response.json()))

                async with anyio.create_task_group() as group:
                    for i in range(24):
                        identity, tenant = ("alice", TENANT_A) if i % 2 == 0 else ("carol", TENANT_B)
                        group.start_soon(one, identity, tenant)
        return results

    results = run(scenario())
    assert len(results) == 24
    for tenant, status, rows in results:
        assert status == 200
        assert len(rows) == (3 if tenant == str(TENANT_A) else 2)
        assert {row[TENANT_COLUMN] for row in rows} == {tenant}


def test_an_unreachable_database_is_503_not_401_or_404():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"postgresql+asyncpg://nobody@127.0.0.1:{port}/none"
    grant = {"tenant_id": TENANT_A, "subject": "alice", "roles": []}
    settings = Settings(app_name="x", api_keys={"key-alice": grant}, database_url=url)
    with TestClient(create_app(settings)) as client:
        response = client.get(url_for(TENANT_A), headers={"X-API-Key": "key-alice"})
    assert response.status_code == 503, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
