"""Generated persistence: the database module, the Alembic environment and the deterministic initial and additive migrations."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

from ..spec import FieldSpec, Spec
from ._base import SA_TYPES, Layout, Lineage, PhaseState
from ._helpers import _call, _entity_index, _fk_fields, _fk_name, _ordered_entities, _rls, _sql_default, _tenant
from .phased import pending_columns, progress_table, progress_table_ddl


def _db(spec: Spec) -> str:
    rls = _rls(spec)
    lines = [
        '"""Async engine, request-scoped sessions and the declarative base (SQLAlchemy 2, asyncpg).',
        "",
        "Tables are created by Alembic migrations only (``alembic upgrade head``); nothing here calls ``create_all``.",
        "The naming convention gives every constraint and index a deterministic name, so the migration scripts and the",
        "models agree and the test suite can compare them.",
    ]
    if rls:
        lines += [
            "",
            "Row-level security: the application connects as a role without BYPASSRLS, and ``get_tenant_session`` sets the",
            "tenant context for the current transaction only (``set_config(..., true)``), from the authenticated principal",
            "and never from the request. The policies see that tenant until commit or rollback; a connection returned to",
            "the pool carries no context (reset on return), so the next request starts closed.",
        ]
    lines += [
        '"""',
        "",
        "from collections.abc import AsyncIterator",
        "from typing import Annotated",
        "",
        "from fastapi import Depends, Request",
        "from sqlalchemy import MetaData" + (", text" if rls else ""),
        "from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine",
        "from sqlalchemy.orm import DeclarativeBase",
    ]
    if rls:
        lines += ["", f"from {spec.package}.core.security import TenantPrincipalDep"]
    lines += [
        "",
        "NAMING_CONVENTION = {",
        '    "ix": "ix_%(column_0_label)s",',
        '    "uq": "uq_%(table_name)s_%(column_0_N_name)s",',
        '    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",',
        '    "pk": "pk_%(table_name)s",',
        "}",
    ]
    if rls:
        lines.append(f'TENANT_CONTEXT_KEY = "{spec.security["multi_tenant"]["rls"]["context_key"]}"  # the GUC the policies read; set per transaction')
    lines += [
        "",
        "",
        "class Base(DeclarativeBase):",
        "    metadata = MetaData(naming_convention=NAMING_CONVENTION)",
        "",
        "",
        "def make_engine(url: str) -> AsyncEngine:",
        "    return create_async_engine(url, pool_pre_ping=True)",
        "",
        "",
        "async def get_session(request: Request) -> AsyncIterator[AsyncSession]:",
        '    """One session per request (unit of work): the service commits; any error rolls the unit back."""',
        "    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory",
        "    async with factory() as session:",
        "        try:",
        "            yield session",
        "        except BaseException:",
        "            await session.rollback()",
        "            raise",
        "",
        "",
        "SessionDep = Annotated[AsyncSession, Depends(get_session)]",
    ]
    if rls:
        lines += [
            "",
            "",
            "async def get_tenant_session(session: SessionDep, principal: TenantPrincipalDep) -> AsyncSession:",
            '    """The request session with the tenant context of the authenticated principal, for this transaction only."""',
            '    params = {"key": TENANT_CONTEXT_KEY, "tenant": str(principal.tenant_id)}',
            '    await session.execute(text("SELECT set_config(:key, :tenant, true)"), params)',
            "    return session",
            "",
            "",
            "TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]",
        ]
    return "\n".join(lines) + "\n"


def _alembic_ini(spec: Spec) -> str:
    prefix = spec.package.upper()
    return f"""# Alembic configuration of {spec.name}. The database URL comes from {prefix}_DATABASE_URL (migrations/env.py);
# the value below is a credential-free fallback for `alembic ... --sql` (offline mode).
[alembic]
script_location = migrations
prepend_sys_path = src
file_template = %%(rev)s_%%(slug)s
sqlalchemy.url = postgresql+asyncpg://localhost:5432/{spec.package}

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
"""


def _alembic_env(spec: Spec, layout: Layout, phases: Optional[PhaseState] = None) -> str:
    prefix = spec.package.upper()
    model_imports = sorted(f"from {layout.imp('models', m.name)} import Base as _base_{m.name}  # noqa: F401" for m in spec.modules)
    if phases is not None and (phases.all_phased or phases.new_phased):
        model_imports.append(f"from {spec.package}.ops.backfill import PROGRESS as _progress_table  # noqa: F401")
    if _rls(spec):
        url_doc = f"URL from {prefix}_MIGRATIONS_DATABASE_URL (the owner)"
        url_body = f'    owner = os.environ.get("{prefix}_MIGRATIONS_DATABASE_URL")\n    return owner or os.environ.get("{prefix}_DATABASE_URL") or config.get_main_option("sqlalchemy.url")'
    else:
        url_doc = f"URL from {prefix}_DATABASE_URL"
        url_body = f'    return os.environ.get("{prefix}_DATABASE_URL") or config.get_main_option("sqlalchemy.url")'
    return f'''"""Alembic environment: async engine, {url_doc},
metadata of every generated model.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from {spec.package}.core.db import Base

# importing the model modules registers their tables on Base.metadata
{chr(10).join(model_imports)}

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
{url_body}


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={{"paramstyle": "named"}},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
'''


def _alembic_mako() -> str:
    return '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''


def _sa_type(f: FieldSpec) -> str:
    if f.type == "str":
        return f"sa.String(length={f.max_length or 255})"
    if f.type == "decimal":
        return "sa.Numeric(precision=18, scale=2)"
    if f.type == "datetime":
        return "sa.DateTime(timezone=True)"
    return f"sa.{SA_TYPES[f.type]}()"


def initial_revision(spec: Spec) -> str:
    """Deterministic revision id of the initial migration: derived from the contract digest, not from the clock."""
    return hashlib.sha1(("initial|" + spec.digest).encode("utf-8")).hexdigest()[:12]


def delta_revision(lineage: Lineage, spec: Spec) -> str:
    """Deterministic revision id of an additive migration: derived from the current head and the evolved contract."""
    return hashlib.sha1((f"delta|{lineage.head}|" + spec.digest).encode("utf-8")).hexdigest()[:12]


def _fk_constraint(table: str, f: FieldSpec, referred: str, tenant: str, sa: str, indent: str) -> List[str]:
    """``ForeignKeyConstraint`` for a reference, composite over the tenant id between tenant tables."""
    cols = [tenant, f.name] if tenant else [f.name]
    refs = [f"{referred}.{c}" if c == tenant else f"{referred}.id" for c in cols]
    args = [json.dumps(cols), json.dumps(refs), f'name="{_fk_name(table, cols, referred)}"', f'ondelete="{f.on_delete.upper().replace("_", " ")}"']
    lines = _call(f"{sa}ForeignKeyConstraint(", args, ")", indent)
    lines[-1] += ","
    return lines


def _column_sql(f: FieldSpec, nullable: Optional[bool] = None) -> str:
    """``nullable`` overrides the field's requirement: a phased column is added nullable and without a default."""
    default = f', server_default=sa.text("{_sql_default(f)}")' if f.has_default and nullable is None else ""
    return f'sa.Column("{f.name}", {_sa_type(f)}{default}, nullable={(not f.required) if nullable is None else nullable})'


def _baseline_columns(lineage: Optional[Lineage]) -> Dict[str, List[str]]:
    """``table -> column names`` already present according to the lineage's baseline contract."""
    if lineage is None or lineage.baseline is None:
        return {}
    out: Dict[str, List[str]] = {}
    for m in lineage.baseline.modules:
        for e in m.entities:
            out[e.plural] = [f.name for f in e.fields]
    return out


def added_columns(spec: Spec, lineage: Optional[Lineage]) -> List[Tuple[str, FieldSpec, Optional[str]]]:
    """``(table, field, referred table)`` for fields of existing tables that the baseline did not have."""
    existing = set(lineage.tables) if lineage else set()
    baseline_cols = _baseline_columns(lineage)
    index = _entity_index(spec)
    out: List[Tuple[str, FieldSpec, Optional[str]]] = []
    for _m, e in _ordered_entities(spec):
        if e.plural not in existing:
            continue
        for f in e.fields:
            if f.name not in baseline_cols.get(e.plural, []):
                referred = index[f.references][1].plural if f.references and f.references in index else None
                out.append((e.plural, f, referred))
    return out


def _migration(spec: Spec, lineage: Optional[Lineage] = None, phases: Optional[PhaseState] = None) -> str:
    """The initial migration (every table), or, with a lineage, the additive migration: the tables the existing
    chain does not create yet plus the columns added to existing tables (``down_revision`` = current head;
    existing files are never touched). Columns added to existing tables are nullable or carry a server default,
    which is the backfill strategy; anything else is refused by the planner before this runs. Under
    ``shared_schema_rls`` every new tenant table is protected in the same revision that creates it (ENABLE + FORCE
    row level security, one USING/WITH CHECK policy on the transaction-local tenant context, row privileges for the
    application role, which must exist and must not bypass RLS)."""
    tenant = _tenant(spec)
    rls = _rls(spec)
    index = _entity_index(spec)
    revision = delta_revision(lineage, spec) if lineage else initial_revision(spec)
    existing = set(lineage.tables) if lineage else set()
    owned = {e.plural for _m, e in spec.owned_entities()}
    up: List[str] = []
    down: List[str] = []
    new_tables = [e.plural for _m, e in _ordered_entities(spec) if e.plural not in existing]
    if rls and new_tables:
        up.append("    op.execute(f\"GRANT USAGE ON SCHEMA public TO {_app_role()}\")")
    for _m, e in _ordered_entities(spec):
        table = e.plural
        if table in existing:
            continue
        cols = ['        sa.Column("id", sa.Uuid(), nullable=False),']
        if tenant:
            cols.append(f'        sa.Column("{tenant}", sa.Uuid(), nullable=False),')
        if table in owned:
            cols.append('        sa.Column("owner_id", sa.String(length=255), nullable=False),')
        for f in e.fields:
            cols.append(f"        {_column_sql(f)},")
        cols.append('        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),')
        cols.append(f'        sa.PrimaryKeyConstraint("id", name="pk_{table}"),')
        if tenant:
            cols.append(f'        sa.UniqueConstraint("{tenant}", "id", name="uq_{table}_{tenant}_id"),')
        for f in _fk_fields(e):
            _tm, te = index[f.references]  # type: ignore[index]
            cols += _fk_constraint(table, f, te.plural, tenant, "sa.", "        ")
        up += ["    op.create_table(", f'        "{table}",', *cols, "    )"]
        if tenant:
            up.append(f'    op.create_index("ix_{table}_{tenant}", "{table}", ["{tenant}"], unique=False)')
        if table in owned:
            up.append(f'    op.create_index("ix_{table}_owner_id", "{table}", ["owner_id"], unique=False)')
        for f in _fk_fields(e):
            up.append(f'    op.create_index("ix_{table}_{f.name}", "{table}", ["{f.name}"], unique=False)')
        if rls:
            up.append(f'    _protect("{table}")')
        drop = [f'    op.drop_table("{table}")']
        for f in reversed(_fk_fields(e)):
            drop.insert(0, f'    op.drop_index("ix_{table}_{f.name}", table_name="{table}")')
        if table in owned:
            drop.insert(0, f'    op.drop_index("ix_{table}_owner_id", table_name="{table}")')
        if tenant:
            drop.insert(0, f'    op.drop_index("ix_{table}_{tenant}", table_name="{table}")')
        down = drop + down
    columns = added_columns(spec, lineage)
    phased_now = pending_columns(phases.new_phased) if phases is not None else frozenset()
    for table, f, referred in columns:
        up.append(f"    op.add_column({json.dumps(table)}, {_column_sql(f, True if (table, f.name) in phased_now else None)})")
        if referred:
            cols = [tenant, f.name] if tenant else [f.name]
            refs = [f"{tenant}" if c == tenant else "id" for c in cols]
            up.append(f'    op.create_foreign_key({json.dumps(_fk_name(table, cols, referred))}, {json.dumps(table)}, {json.dumps(referred)}, {json.dumps(cols)}, {json.dumps(refs)}, ondelete="{f.on_delete.upper().replace("_", " ")}")')
            up.append(f'    op.create_index("ix_{table}_{f.name}", "{table}", ["{f.name}"], unique=False)')
    for table, f, referred in reversed(columns):
        if referred:
            down.insert(0, f'    op.drop_column("{table}", "{f.name}")')
            down.insert(0, f'    op.drop_constraint({json.dumps(_fk_name(table, [tenant, f.name] if tenant else [f.name], referred))}, "{table}", type_="foreignkey")')
            down.insert(0, f'    op.drop_index("ix_{table}_{f.name}", table_name="{table}")')
        else:
            down.insert(0, f'    op.drop_column("{table}", "{f.name}")')
    progress = progress_table(spec)
    if phased_now and lineage is not None and progress not in set(lineage.tables):
        up += progress_table_ddl(spec)
        down.insert(0, f'    op.drop_table("{progress}")')
    changes = new_tables + [f"{t}.{f.name}" for t, f, _r in columns]
    expand_title = f"Expand {', '.join(changes)} in phases"
    if len(expand_title) > 60:
        expand_title = f"Expand {len(changes)} column(s) in phases"
    title = f"Initial schema of {spec.name}" if not lineage else (expand_title if phased_now else f"Add {', '.join(changes)} to {spec.name}")
    imports = ["import sqlalchemy as sa", "from alembic import op"]
    helpers: List[str] = []
    if rls:
        prefix = spec.package.upper()
        rls_cfg = spec.security["multi_tenant"]["rls"]
        imports = ["import os", "import re", "", "import sqlalchemy as sa", "from alembic import context, op"]
        helpers = [
            "",
            f'APP_ROLE = os.environ.get("{prefix}_APP_ROLE", "{rls_cfg["app_role"]}")',
            f'CONTEXT_KEY = "{rls_cfg["context_key"]}"',
            "",
            "",
            "def _app_role() -> str:",
            '    """The application role, verified on the live connection: it must exist and must not be able to bypass row-level',
            '    security (superuser or BYPASSRLS). Creating roles is a provisioning step, not a migration: the owner that runs the',
            '    migrations may not have CREATEROLE, and a role created here with a default password would be a credential."""',
            '    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", APP_ROLE):',
            f'        raise RuntimeError(f"{prefix}_APP_ROLE {{APP_ROLE!r}} is not a plain role name")',
            "    if context.is_offline_mode():",
            '        raise RuntimeError("row-level security migrations need a live connection to verify the application role")',
            '    query = sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name")',
            '    row = op.get_bind().execute(query, {"name": APP_ROLE}).first()',
            "    if row is None:",
            "        raise RuntimeError(",
            '            f"role {APP_ROLE} does not exist; create it first: "',
            '            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD \'...\' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"',
            "        )",
            "    if row.rolsuper or row.rolbypassrls:",
            '        raise RuntimeError(f"role {APP_ROLE} would bypass row-level security (superuser or BYPASSRLS); refusing")',
            "    return APP_ROLE",
            "",
            "",
            "def _protect(table: str) -> None:",
            '    """Row-level security for a tenant table: enabled and forced (the owner is subject to it too), one policy that both',
            "    filters reads (USING) and checks writes (WITH CHECK) against the transaction-local tenant context; a NULL or empty",
            '    context matches nothing. The application role gets row privileges only: no DDL, no TRUNCATE, no ownership."""',
            "    role = _app_role()",
            f'    predicate = f"{tenant} = NULLIF(current_setting(\'{{CONTEXT_KEY}}\', true), \'\')::uuid"',
            '    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {role}")',
            '    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")',
            '    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")',
            '    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})")',
        ]
    lines = [
        *([f'"""{title}.', '', f'Generated from the architecture contract (digest {spec.digest[:12]}).'] if phased_now else [f'"""{title}: generated from the architecture contract (digest {spec.digest[:12]}).']),
        "",
        f"Revision ID: {revision}",
        f"Revises: {lineage.head if lineage else 'none (initial)'}",
        "Create Date: derived from the contract, not from the clock (reproducible)",
        '"""',
        "",
        *imports,
        "",
        f'revision = "{revision}"',
        f"down_revision = {json.dumps(lineage.head) if lineage else 'None'}",
        "branch_labels = None",
        "depends_on = None",
        *helpers,
        "",
        "",
        "def upgrade() -> None:",
        *(up or ["    pass"]),
        "",
        "",
        "def downgrade() -> None:",
        *(down or ["    pass"]),
    ]
    return "\n".join(lines) + "\n"


def migration_file(spec: Spec, lineage: Optional[Lineage] = None, phases: Optional[PhaseState] = None) -> str:
    if lineage is None:
        return f"migrations/versions/{initial_revision(spec)}_initial_schema.py"
    new_tables = [e.plural for _m, e in _ordered_entities(spec) if e.plural not in set(lineage.tables)]
    changes = new_tables + [f"{t}_{f.name}" for t, f, _r in added_columns(spec, lineage)]
    verb = "expand_" if (phases is not None and phases.new_phased) else "add_"
    slug = verb + "_".join(changes) if changes else "noop"
    return f"migrations/versions/{delta_revision(lineage, spec)}_{slug[:60]}.py"
