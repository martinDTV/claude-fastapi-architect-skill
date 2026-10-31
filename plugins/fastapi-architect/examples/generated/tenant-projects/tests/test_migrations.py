"""Migrations: the migrated schema is exactly what the models declare, constraints exist, and up/down is repeatable."""

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tenant_projects.core.db import Base
from tests.conftest import PROJECT_ROOT, alembic, run, table_names

REVISION = "2b11be23cf88"
TABLES = ["projects", "tasks"]
FOREIGN_KEYS = [("tasks", "fk_tasks_tenant_id_project_id_projects")]


async def _inspect(url: str, what):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(what)
    finally:
        await engine.dispose()


def test_the_script_directory_has_one_head():
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    assert script.get_heads() == [REVISION]


def test_migrated_schema_matches_the_models(migrated: str):
    """No drift: autogenerate would produce nothing (unexpected schema changes are detected here)."""

    def diff(sync):
        context = MigrationContext.configure(sync, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    assert run(_inspect(migrated, diff)) == []


def test_tables_and_constraints_exist(migrated: str):
    assert sorted(set(run(table_names(migrated))) - {"alembic_version"}) == TABLES

    def constraints(sync):
        inspector = inspect(sync)
        found = []
        for table in TABLES:
            found += [(table, fk["name"]) for fk in inspector.get_foreign_keys(table)]
            unique = [sorted(uq["column_names"]) for uq in inspector.get_unique_constraints(table)]
            assert ["id", "tenant_id"] in unique, (table, unique)
        return found

    found = run(_inspect(migrated, constraints))
    for expected in FOREIGN_KEYS:
        assert expected in found, (expected, found)


def test_upgrade_and_downgrade_are_repeatable(migrated: str):
    assert alembic(migrated, "downgrade", "base").returncode == 0
    assert run(table_names(migrated)) == ["alembic_version"]
    assert alembic(migrated, "upgrade", "head").returncode == 0
    assert sorted(set(run(table_names(migrated))) - {"alembic_version"}) == TABLES
    current = alembic(migrated, "current")
    assert current.returncode == 0 and REVISION in current.stdout + current.stderr
