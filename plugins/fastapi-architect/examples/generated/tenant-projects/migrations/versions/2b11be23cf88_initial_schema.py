"""Initial schema of tenant-projects: generated from the architecture contract (digest 90251d20a00e).

Revision ID: 2b11be23cf88
Revises: none (initial)
Create Date: derived from the contract, not from the clock (reproducible)
"""

import os
import re

import sqlalchemy as sa
from alembic import context, op

revision = "2b11be23cf88"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("TENANT_PROJECTS_APP_ROLE", "tenant_projects_app")
CONTEXT_KEY = "app.tenant_id"


def _app_role() -> str:
    """The application role, verified on the live connection: it must exist and must not be able to bypass row-level
    security (superuser or BYPASSRLS). Creating roles is a provisioning step, not a migration: the owner that runs the
    migrations may not have CREATEROLE, and a role created here with a default password would be a credential."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", APP_ROLE):
        raise RuntimeError(f"TENANT_PROJECTS_APP_ROLE {APP_ROLE!r} is not a plain role name")
    if context.is_offline_mode():
        raise RuntimeError("row-level security migrations need a live connection to verify the application role")
    query = sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name")
    row = op.get_bind().execute(query, {"name": APP_ROLE}).first()
    if row is None:
        raise RuntimeError(
            f"role {APP_ROLE} does not exist; create it first: "
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"
        )
    if row.rolsuper or row.rolbypassrls:
        raise RuntimeError(f"role {APP_ROLE} would bypass row-level security (superuser or BYPASSRLS); refusing")
    return APP_ROLE


def _protect(table: str) -> None:
    """Row-level security for a tenant table: enabled and forced (the owner is subject to it too), one policy that both
    filters reads (USING) and checks writes (WITH CHECK) against the transaction-local tenant context; a NULL or empty
    context matches nothing. The application role gets row privileges only: no DDL, no TRUNCATE, no ownership."""
    role = _app_role()
    predicate = f"tenant_id = NULLIF(current_setting('{CONTEXT_KEY}', true), '')::uuid"
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {role}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})")


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_app_role()}")
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("budget", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"], unique=False)
    _protect("projects")
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_tasks_tenant_id_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"], unique=False)
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"], unique=False)
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"], unique=False)
    _protect("tasks")


def downgrade() -> None:
    op.drop_index("ix_tasks_tenant_id", table_name="tasks")
    op.drop_index("ix_tasks_owner_id", table_name="tasks")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_table("projects")
