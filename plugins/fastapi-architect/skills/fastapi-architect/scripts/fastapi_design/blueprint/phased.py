"""Phased PostgreSQL migrations (v2.1, increment 2): expand, backfill, verify, contract.

A field declared with a ``backfill`` in an evolution contract is a **phased change**. This module renders what the
generator adds to a project for it, and nothing else in the project is touched by hand:

* the **expand** revision adds the column nullable and without a server default (compatible with the running
  application; the backfill fills existing rows) and creates the progress table the first time;
* the **index** revision creates ``CREATE INDEX CONCURRENTLY`` outside the migration transaction, checks the
  existing definition instead of trusting ``IF NOT EXISTS``, drops an invalid leftover and recreates it, and refuses
  to end with an invalid index;
* the **backfill runner** (``src/<pkg>/ops/backfill.py``) processes rows in keyset batches ordered by the primary key,
  one transaction per batch that writes the rows and the checkpoint together, ``FOR UPDATE SKIP LOCKED`` so it never
  waits for the application, never overwrites a value that is not NULL, holds an advisory lock so two workers cannot
  run the same operation, refuses a checkpoint whose checksum belongs to another definition, refuses to run on a
  table with row-level security unless the role bypasses it (otherwise it would silently see no rows), and calls an
  operation complete only after counting zero pending rows at the end of its last pass;
* the **verify** command of the runner writes the migration evidence (``fastapi-architect/migration-evidence/1.0``):
  the alembic head applied, the column's nullability, the pending rows, the progress record, the indexes and their
  validity; the planner consumes it before it accepts a contract;
* the **contract** revision re-checks the pending rows and the progress state in the database, then performs the
  online NOT NULL transition statement by statement in autocommit mode (``ADD CONSTRAINT ... NOT VALID``,
  ``VALIDATE CONSTRAINT``, ``SET NOT NULL``, ``DROP CONSTRAINT``), each step idempotent so a re-run after an
  interruption continues; its downgrade is ``DROP NOT NULL`` (no data is lost).

Every identifier in the generated SQL comes from names the contract validated as Python identifiers; values come
from typed literals bound as parameters. No SQL text travels from the contract to the database.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence

from ..spec import Spec
from ._helpers import _ordered_entities

BACKFILL_STRATEGIES = ("constant", "copy_column", "uuid", "now")
EVIDENCE_SCHEMA = "fastapi-architect/migration-evidence/1.0"
PHASE_EXPAND = "expand"
PHASE_INDEX = "index"
PHASE_CONTRACT = "contract"


def progress_table(spec: Spec) -> str:
    return f"{spec.package}_backfill_progress"


def operation_id(table: str, column: str, strategy: str, params: Dict[str, object], head: str) -> str:
    """Stable identity of a phased change: table, column, strategy, parameters and the head it was declared on."""
    payload = json.dumps({"table": table, "column": column, "strategy": strategy, "params": params, "head": head}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def definition_checksum(change: Dict[str, object]) -> str:
    """What the runner stores next to the checkpoint and compares before resuming."""
    keys = ("table", "column", "field_type", "strategy", "params")
    return hashlib.sha256(json.dumps({k: change.get(k) for k in keys}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def index_name(table: str, columns: Sequence[str]) -> str:
    return f"ix_{table}_{'_'.join(columns)}"


def index_revision(head: str, indexes: Sequence[Dict[str, object]]) -> str:
    payload = json.dumps(sorted((str(i["table"]), list(i["columns"]), bool(i.get("unique"))) for i in indexes), separators=(",", ":"))
    return hashlib.sha1(f"index|{head}|{payload}".encode("utf-8")).hexdigest()[:12]


def contract_revision(head: str, operations: Sequence[str]) -> str:
    return hashlib.sha1(f"contract|{head}|{','.join(sorted(operations))}".encode("utf-8")).hexdigest()[:12]


def pending_columns(pending: Sequence[Dict[str, object]]) -> frozenset:
    return frozenset((str(c["table"]), str(c["column"])) for c in pending)


def _literal(value: object) -> str:
    """A Python literal for generated modules (JSON would write null/true/false)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_literal(k)}: {_literal(value[k])}" for k in sorted(value, key=str)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return repr(value)


# --------------------------------------------------------------------------- #
# revisions
# --------------------------------------------------------------------------- #


def _revision_header(title: str, detail: str, revision: str, down: str, imports: List[str], spec: Spec) -> List[str]:
    return [
        f'"""{title}',
        "",
        detail,
        f"Generated from the architecture contract (digest {spec.digest[:12]}).",
        "",
        f"Revision ID: {revision}",
        f"Revises: {down}",
        "Create Date: derived from the contract, not from the clock (reproducible)",
        '"""',
        "",
        *imports,
        "",
        f'revision = "{revision}"',
        f'down_revision = "{down}"',
        "branch_labels = None",
        "depends_on = None",
    ]


def progress_table_ddl(spec: Spec) -> List[str]:
    """``op.create_table`` lines of the progress table (expand revision that introduces the first phased change)."""
    table = progress_table(spec)
    return [
        "    op.create_table(",
        f'        "{table}",',
        '        sa.Column("op_id", sa.String(length=64), nullable=False),',
        '        sa.Column("table_name", sa.String(length=63), nullable=False),',
        '        sa.Column("column_name", sa.String(length=63), nullable=False),',
        '        sa.Column("strategy", sa.String(length=32), nullable=False),',
        '        sa.Column("checksum", sa.String(length=64), nullable=False),',
        '        sa.Column("state", sa.String(length=16), server_default=sa.text("\'running\'"), nullable=False),',
        '        sa.Column("last_key", sa.Text(), nullable=True),',
        '        sa.Column("rows_processed", sa.BigInteger(), server_default=sa.text("0"), nullable=False),',
        '        sa.Column("batches", sa.Integer(), server_default=sa.text("0"), nullable=False),',
        '        sa.Column("passes", sa.Integer(), server_default=sa.text("0"), nullable=False),',
        '        sa.Column("worker", sa.String(length=128), nullable=True),',
        '        sa.Column("last_error", sa.Text(), nullable=True),',
        '        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),',
        '        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),',
        '        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),',
        f'        sa.PrimaryKeyConstraint("op_id", name="pk_{table}"),',
        "    )",
    ]


def index_migration(spec: Spec, head: str, indexes: Sequence[Dict[str, object]]) -> str:
    """One non-transactional revision per evolution for the concurrent indexes on existing tables."""
    revision = index_revision(head, indexes)
    names = ", ".join(index_name(str(i["table"]), list(i["columns"])) for i in indexes)
    lines = _revision_header(
        f"Concurrent index(es): {names}.",
        "CREATE INDEX CONCURRENTLY outside the migration transaction; the definition of an existing index is checked,\nan invalid leftover is dropped and rebuilt, and the revision fails rather than end with an invalid index.",
        revision,
        head,
        ["import sqlalchemy as sa", "from alembic import op"],
        spec,
    )
    lines += [
        "",
        "INDEX_STATE = sa.text(",
        '    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "',
        '    "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace "',
        '    "WHERE c.relname = :name AND n.nspname = current_schema()"',
        ")",
        "",
        "",
        "def _index_state(name: str) -> tuple[bool, str] | None:",
        '    """``(valid, definition)`` of an existing index, or None."""',
        '    row = op.get_bind().execute(INDEX_STATE, {"name": name}).first()',
        "    return None if row is None else (bool(row[0]), str(row[1]))",
        "",
        "",
        "def _create_index_concurrently(name: str, table: str, columns: list[str], unique: bool) -> None:",
        '    """The checks IF NOT EXISTS does not make: another definition under the same name is a conflict (refused),',
        "    an invalid leftover of an interrupted build is dropped and rebuilt, a valid identical index is kept, and the",
        '    revision fails if the index is not valid when it ends. Safe to re-run."""',
        '    kind = "UNIQUE INDEX" if unique else "INDEX"',
        '    schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar()',
        '    expected = f"CREATE {kind} {name} ON {schema}.{table} USING btree ({\', \'.join(columns)})"',
        "    state = _index_state(name)",
        "    if state is not None:",
        "        valid, definition = state",
        "        if definition != expected:",
        '            message = f"index {name} exists with a different definition: {definition!r}; expected {expected!r}"',
        "            raise RuntimeError(message)",
        "        if valid:",
        "            return",
        '        op.execute(f\'DROP INDEX CONCURRENTLY IF EXISTS "{name}"\')  # invalid: an interrupted or failed build',
        '    quoted = ", ".join(f\'"{c}"\' for c in columns)',
        '    op.execute(f\'CREATE {kind} CONCURRENTLY "{name}" ON "{table}" ({quoted})\')',
        "    state = _index_state(name)",
        "    if state is None or not state[0]:",
        '        raise RuntimeError(f"index {name} is not valid after its creation; re-run the migration to rebuild it")',
        "",
        "",
        "def upgrade() -> None:",
        "    with op.get_context().autocommit_block():  # CREATE INDEX CONCURRENTLY cannot run inside a transaction",
    ]
    for i in indexes:
        name = index_name(str(i["table"]), list(i["columns"]))
        lines.append(f"        _create_index_concurrently({json.dumps(name)}, {json.dumps(str(i['table']))}, {json.dumps(list(i['columns']))}, {bool(i.get('unique'))})")
    lines += ["", "", "def downgrade() -> None:", "    with op.get_context().autocommit_block():"]
    for i in reversed(list(indexes)):
        lines.append(f'        op.execute(\'DROP INDEX CONCURRENTLY IF EXISTS "{index_name(str(i["table"]), list(i["columns"]))}"\')')
    return "\n".join(lines) + "\n"


def index_migration_file(head: str, indexes: Sequence[Dict[str, object]]) -> str:
    slug = "_".join(index_name(str(i["table"]), list(i["columns"])) for i in indexes)
    return f"migrations/versions/{index_revision(head, indexes)}_concurrent_{slug[:50]}.py"


def contract_migration(spec: Spec, head: str, contracting: Sequence[Dict[str, object]]) -> str:
    """The NOT NULL transition of completed backfills, online and idempotent, after re-checking the database."""
    revision = contract_revision(head, [str(c["op_id"]) for c in contracting])
    targets = ", ".join(f"{c['table']}.{c['column']}" for c in contracting)
    lines = _revision_header(
        f"Contract: {targets} become NOT NULL.",
        "Each statement runs in its own transaction: ADD CONSTRAINT ... NOT VALID and SET NOT NULL take brief ACCESS\nEXCLUSIVE locks, VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE (writes continue during the scan). The\nprecondition is checked again on the live database first. A re-run after an interruption continues.",
        revision,
        head,
        ["import sqlalchemy as sa", "from alembic import op"],
        spec,
    )
    lines += [
        "",
        f'PROGRESS_TABLE = "{progress_table(spec)}"',
        "",
        "",
        "def _require_complete(op_id: str, table: str, column: str) -> None:",
        '    """The precondition the plan checked from the evidence, checked again here: no pending row, backfill complete."""',
        "    bind = op.get_bind()",
        "    pending = bind.execute(sa.text(f'SELECT count(*) FROM \"{table}\" WHERE \"{column}\" IS NULL')).scalar()",
        "    if pending:",
        '        raise RuntimeError(f"{table}.{column}: {pending} row(s) still NULL; complete the backfill {op_id} before contracting")',
        '    state = bind.execute(sa.text(f"SELECT state FROM {PROGRESS_TABLE} WHERE op_id = :op"), {"op": op_id}).scalar()',
        '    if state == "complete":',
        "        return",
        "    total = bind.execute(sa.text(f'SELECT count(*) FROM \"{table}\"')).scalar()",
        "    if state is None and total == 0:  # a fresh environment: nothing to backfill, the fact is recorded",
        "        bind.execute(",
        "            sa.text(",
        '                f"INSERT INTO {PROGRESS_TABLE} (op_id, table_name, column_name, strategy, checksum, state, finished_at) "',
        "                \"VALUES (:op, :t, :c, 'none', 'empty-table-at-contract', 'complete', now()) ON CONFLICT (op_id) DO NOTHING\"",  # the runner's CONTRACT_SENTINEL
        "            ),",
        '            {"op": op_id, "t": table, "c": column},',
        "        )",
        "        return",
        '    raise RuntimeError(f"backfill {op_id} is {state!r}, not complete; the contract phase needs a complete backfill")',
        "",
        "",
        "def _set_not_null_online(table: str, column: str) -> None:",
        '    """ADD CONSTRAINT NOT VALID (no scan), VALIDATE CONSTRAINT (writes continue), SET NOT NULL (PostgreSQL 12+ proves',
        '    it from the validated constraint: no second scan), DROP CONSTRAINT. Idempotent: every step checks the catalog."""',
        '    check = f"ck_{table}_{column}_not_null"',
        "    bind = op.get_bind()",
        "    nullable = bind.execute(",
        '        sa.text(',
        '            "SELECT is_nullable FROM information_schema.columns "',
        '            "WHERE table_name = :t AND column_name = :c AND table_schema = current_schema()"',
        "        ),",
        '        {"t": table, "c": column},',
        "    ).scalar()",
        '    if nullable == "NO":',
        "        op.execute(f'ALTER TABLE \"{table}\" DROP CONSTRAINT IF EXISTS \"{check}\"')",
        "        return",
        '    validated = bind.execute(sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"), {"n": check}).scalar()',
        "    if validated is None:",
        "        op.execute(f'ALTER TABLE \"{table}\" ADD CONSTRAINT \"{check}\" CHECK (\"{column}\" IS NOT NULL) NOT VALID')",
        "        validated = False",
        "    if not validated:",
        "        op.execute(f'ALTER TABLE \"{table}\" VALIDATE CONSTRAINT \"{check}\"')",
        "    op.execute(f'ALTER TABLE \"{table}\" ALTER COLUMN \"{column}\" SET NOT NULL')",
        "    op.execute(f'ALTER TABLE \"{table}\" DROP CONSTRAINT IF EXISTS \"{check}\"')",
        "",
        "",
        "def upgrade() -> None:",
        "    with op.get_context().autocommit_block():  # one transaction per statement",
    ]
    for c in contracting:
        lines.append(f'        _require_complete({json.dumps(str(c["op_id"]))}, {json.dumps(str(c["table"]))}, {json.dumps(str(c["column"]))})')
    for c in contracting:
        lines.append(f'        _set_not_null_online({json.dumps(str(c["table"]))}, {json.dumps(str(c["column"]))})')
    lines += ["", "", "def downgrade() -> None:", "    with op.get_context().autocommit_block():"]
    for c in reversed(list(contracting)):
        lines.append(f"        op.execute('ALTER TABLE \"{c['table']}\" ALTER COLUMN \"{c['column']}\" DROP NOT NULL')")
    return "\n".join(lines) + "\n"


def contract_migration_file(head: str, contracting: Sequence[Dict[str, object]]) -> str:
    slug = "_".join(f"{c['table']}_{c['column']}" for c in contracting)
    return f"migrations/versions/{contract_revision(head, [str(c['op_id']) for c in contracting])}_contract_{slug[:50]}.py"


# --------------------------------------------------------------------------- #
# the generated runner, its declarative definitions and its tests
# --------------------------------------------------------------------------- #


def backfill_specs(spec: Spec, changes: Sequence[Dict[str, object]], indexes: Sequence[Dict[str, object]], head: str) -> str:
    """``src/<pkg>/ops/backfills.py``: every phased change the project ever declared (complete ones included), the
    concurrent indexes and the head the runner expects to find applied."""
    lines = [
        '"""Phased changes of this project, generated by FastAPI Architect from the evolution contracts. The runner in',
        "``backfill.py`` reads these definitions; nothing here is SQL and nothing here is edited by hand.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"EXPECTED_HEAD = {json.dumps(head)}",
        "",
        "BACKFILLS: dict[str, dict] = {",
    ]
    keys = ("table", "column", "field_type", "strategy", "params", "state_declared", "expand_revision", "index_revision", "contract_revision")
    for c in sorted(changes, key=lambda x: str(x["op_id"])):
        lines.append(f"    {json.dumps(str(c['op_id']))}: {{")
        for key in keys:
            lines.append(f"        {_literal(key)}: {_literal(c.get(key))},")
        lines.append(f"        'checksum': {_literal(definition_checksum(c))},")
        lines.append("    },")
    lines += ["}", "", "INDEXES: list[dict] = ["]
    for i in indexes:
        entry = {"name": index_name(str(i["table"]), list(i["columns"])), "table": str(i["table"]), "columns": list(i["columns"]), "unique": bool(i.get("unique"))}
        lines.append(f"    {_literal(entry)},")
    lines += ["]", ""]
    return "\n".join(lines)


def backfill_runner(spec: Spec) -> str:
    """``src/<pkg>/ops/backfill.py``: the resumable batch runner and the evidence writer (SQLAlchemy async, asyncpg)."""
    pkg = spec.package
    prefix = pkg.upper()
    return f'''"""Backfill runner and migration evidence for phased changes (generated by FastAPI Architect v2.1).

``python -m {pkg}.ops.backfill run OP_ID [--batch-size N] [--max-batches N] [--max-seconds S]
[--batch-pause-ms M]`` fills the NULL values of a column added by an expand revision, in keyset batches ordered by
the primary key, one transaction per batch that writes the rows and the checkpoint together. ``status OP_ID``
prints the checkpoint.
``verify [--application-version DIGEST] [--output FILE]`` writes the migration evidence the planner reads before it
accepts a contract phase.

Guarantees (each one tested): a row that already has a value is never overwritten; rows the application holds
locked are skipped and revisited in the next pass (FOR UPDATE SKIP LOCKED); a crash before a commit loses nothing,
a crash after a commit resumes from the committed checkpoint; two workers cannot run the same operation (advisory
lock plus a worker record); a checkpoint whose definition checksum differs from the current definition is refused;
the runner refuses a table with row-level security unless the role bypasses it; ``complete`` means a count of zero
pending rows observed after the last pass. Not guaranteed: that no NULL appears afterwards while an application
version that does not write the column is still running; the contract revision re-checks and refuses in that case,
and a later ``run`` re-counts a complete operation and reopens it for the rows that appeared.

Credentials come from ``{prefix}_MIGRATIONS_DATABASE_URL`` (the owner) or ``{prefix}_DATABASE_URL``;
nothing is embedded.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Table, Text, text
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from {pkg}.core.db import Base
from {pkg}.ops.backfills import BACKFILLS, EXPECTED_HEAD, INDEXES

PROGRESS_TABLE = "{progress_table(spec)}"
PROGRESS = Table(
    PROGRESS_TABLE,
    Base.metadata,
    Column("op_id", String(64), primary_key=True),
    Column("table_name", String(63), nullable=False),
    Column("column_name", String(63), nullable=False),
    Column("strategy", String(32), nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("state", String(16), nullable=False, server_default=text("'running'")),
    Column("last_key", Text, nullable=True),
    Column("rows_processed", BigInteger, nullable=False, server_default=text("0")),
    Column("batches", Integer, nullable=False, server_default=text("0")),
    Column("passes", Integer, nullable=False, server_default=text("0")),
    Column("worker", String(128), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)
EVIDENCE_SCHEMA = "{EVIDENCE_SCHEMA}"
CONTRACT_SENTINEL = "empty-table-at-contract"  # the checksum the contract revision records for an empty table
EXIT_OK, EXIT_USAGE, EXIT_CONNECTION, EXIT_LOCKED = 0, 1, 2, 3
EXIT_CHECKPOINT, EXIT_RLS, EXIT_INCOMPLETE, EXIT_UNKNOWN = 4, 5, 6, 7
MAX_PASSES = 3
IDENTIFIER = set("abcdefghijklmnopqrstuvwxyz0123456789_")
PENDING_IDS = (
    "SELECT id FROM {{table}} WHERE {{column}} IS NULL AND (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid)) "
    "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
)
RLS_STATE = (
    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
    "WHERE relname = :t AND relnamespace = current_schema()::regnamespace"
)
COLUMN_STATE = (
    "SELECT is_nullable FROM information_schema.columns "
    "WHERE table_name = :t AND column_name = :c AND table_schema = current_schema()"
)
INDEX_STATE = (
    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
)
ROLE_STATE = "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
PROGRESS_ROW = (
    "SELECT state, last_key, rows_processed, batches, passes, checksum, table_name, column_name "
    "FROM {{table}} WHERE op_id = :op FOR UPDATE"
)


class BackfillError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def database_url() -> str:
    url = os.environ.get("{prefix}_MIGRATIONS_DATABASE_URL") or os.environ.get("{prefix}_DATABASE_URL")
    if not url:
        raise BackfillError(
            EXIT_CONNECTION, "set {prefix}_MIGRATIONS_DATABASE_URL (the owner) or {prefix}_DATABASE_URL"
        )
    return url


def _engine(url: str) -> AsyncEngine:
    """One engine per command invocation (disposed by the caller); never per batch."""
    return create_async_engine(url, poolclass=None)


@asynccontextmanager
async def _connect(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Connect once; whatever the driver raises while connecting is a connection failure (exit code 2)."""
    try:
        conn = await engine.connect()
    except Exception as exc:  # asyncpg raises its own classes here; SQLAlchemy wraps them only once connected
        raise BackfillError(EXIT_CONNECTION, f"database connection failed: {{exc}}") from exc
    try:
        yield conn
    finally:
        await conn.close()


def q(name: str) -> str:
    """Quote an identifier the generator already validated; refuse anything else."""
    if not name or len(name) > 63 or not set(name) <= IDENTIFIER or name[0].isdigit():
        raise BackfillError(EXIT_USAGE, f"invalid identifier {{name!r}}")
    return f'"{{name}}"'


def typed_value(field_type: str, value: Any) -> Any:
    if field_type == "decimal":
        return Decimal(str(value))
    if field_type == "uuid":
        return uuid.UUID(str(value))
    if field_type in ("datetime", "date"):
        return datetime.fromisoformat(str(value))
    return value


def value_expression(definition: dict) -> tuple[str, dict]:
    strategy, params = definition["strategy"], definition.get("params") or {{}}
    if strategy == "constant":
        return ":value", {{"value": typed_value(definition["field_type"], params["value"])}}
    if strategy == "copy_column":
        return q(str(params["source"])), {{}}
    if strategy == "uuid":
        return "gen_random_uuid()", {{}}
    if strategy == "now":
        return "now()", {{}}
    raise BackfillError(EXIT_USAGE, f"unknown strategy {{strategy!r}}")


def checksum_of(definition: dict) -> str:
    keys = ("table", "column", "field_type", "strategy", "params")
    payload = json.dumps({{k: definition.get(k) for k in keys}}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonable(data: dict) -> dict:
    out = {{}}
    for key, value in data.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, (uuid.UUID, Decimal)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


async def _lock(conn: AsyncConnection, op_id: str) -> bool:
    result = await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {{"k": f"backfill:{{op_id}}"}})
    return bool(result.scalar())


async def _rls_guard(conn: AsyncConnection, table: str) -> None:
    row = (await conn.execute(text(RLS_STATE), {{"t": table}})).first()
    if row is None:
        raise BackfillError(EXIT_UNKNOWN, f"table {{table}} does not exist in the current schema")
    if row[0] or row[1]:
        role = (await conn.execute(text(ROLE_STATE))).first()
        if not (role and (role[0] or role[1])):
            user = (await conn.execute(text("SELECT current_user"))).scalar()
            raise BackfillError(
                EXIT_RLS,
                f"{{table}} has row-level security; as {{user}} the backfill would see no rows. "
                "Run it as a role that bypasses RLS (the owner or BYPASSRLS)",
            )


async def _ensure_progress(conn: AsyncConnection, op_id: str, definition: dict, worker: str) -> dict:
    checksum = checksum_of(definition)
    await conn.execute(
        text(
            f"INSERT INTO {{PROGRESS_TABLE}} (op_id, table_name, column_name, strategy, checksum, worker) "
            "VALUES (:op, :t, :c, :s, :k, :w) ON CONFLICT (op_id) DO NOTHING"
        ),
        {{
            "op": op_id,
            "t": definition["table"],
            "c": definition["column"],
            "s": definition["strategy"],
            "k": checksum,
            "w": worker,
        }},
    )
    row = (await conn.execute(text(PROGRESS_ROW.format(table=PROGRESS_TABLE)), {{"op": op_id}})).mappings().first()
    assert row is not None
    same_definition = row["table_name"] == definition["table"] and row["column_name"] == definition["column"]
    if row["checksum"] == CONTRACT_SENTINEL and same_definition:
        return dict(row)  # a fresh environment: the contract revision found an empty table and recorded it as complete
    if row["checksum"] != checksum or not same_definition:
        raise BackfillError(
            EXIT_CHECKPOINT,
            f"checkpoint of {{op_id}} belongs to another definition "
            f"(checksum {{row['checksum'][:12]}} vs {{checksum[:12]}}): refusing to resume from it",
        )
    if row["last_key"] is not None:
        try:
            uuid.UUID(str(row["last_key"]))
        except ValueError as exc:
            message = f"checkpoint of {{op_id}} is corrupt: last_key {{row['last_key']!r}} is not a primary key"
            raise BackfillError(EXIT_CHECKPOINT, message) from exc
    return dict(row)


async def _batch(
    conn: AsyncConnection,
    table_name: str,
    column: str,
    expression: str,
    params: dict,
    last_key: str | None,
    n: int,
    op_id: str,
) -> tuple[int, str | None]:
    """One transaction: select the next NULL keys after the checkpoint, fill them, advance the checkpoint."""
    async with conn.begin():
        select = text(PENDING_IDS.format(table=table_name, column=column))
        ids = [row[0] for row in (await conn.execute(select, {{"last": last_key, "n": n}})).all()]
        if not ids:
            return 0, last_key
        update = text(f"UPDATE {{table_name}} SET {{column}} = {{expression}} WHERE id = ANY(:ids) AND {{column}} IS NULL")
        result = await conn.execute(update, {{**params, "ids": ids}})
        new_key = str(max(ids))
        await conn.execute(
            text(
                f"UPDATE {{PROGRESS_TABLE}} SET last_key = :k, rows_processed = rows_processed + :n, "
                "batches = batches + 1, updated_at = now() WHERE op_id = :op"
            ),
            {{"k": new_key, "n": result.rowcount, "op": op_id}},
        )
        return int(result.rowcount), new_key


async def _set_state(conn: AsyncConnection, op_id: str, state: str, error: str | None = None) -> None:
    async with conn.begin():
        finished = ", finished_at = now()" if state == "complete" else ""
        sql = f"UPDATE {{PROGRESS_TABLE}} SET state = :s, last_error = :e, updated_at = now(){{finished}} WHERE op_id = :op"
        await conn.execute(text(sql), {{"s": state, "e": error, "op": op_id}})


async def run_backfill(
    op_id: str,
    url: str,
    *,
    batch_size: int = 500,
    max_batches: int | None = None,
    max_seconds: float | None = None,
    batch_pause_ms: int = 0,
) -> dict:
    definition = BACKFILLS.get(op_id)
    if definition is None:
        known = ", ".join(sorted(BACKFILLS)) or "none"
        raise BackfillError(EXIT_UNKNOWN, f"unknown operation {{op_id}} (known: {{known}})")
    if batch_size < 1 or batch_size > 50_000:
        raise BackfillError(EXIT_USAGE, "batch size must be between 1 and 50000")
    table_name, column = q(str(definition["table"])), q(str(definition["column"]))
    expression, params = value_expression(definition)
    worker = f"{{socket.gethostname()}}:{{os.getpid()}}"
    started = time.monotonic()
    report: dict[str, Any] = {{
        "operation": op_id,
        "table": definition["table"],
        "column": definition["column"],
        "strategy": definition["strategy"],
        "batch_size": batch_size,
        "batches_this_run": 0,
        "rows_this_run": 0,
        "passes_this_run": 0,
        "worker": worker,
    }}
    engine = _engine(url)
    try:
        async with _connect(engine) as conn:
            if not await _lock(conn, op_id):
                raise BackfillError(EXIT_LOCKED, f"another worker holds the lock of {{op_id}}; one worker per operation")
            await _rls_guard(conn, str(definition["table"]))
            await conn.commit()  # ends the autobegun transaction; the session-level advisory lock survives it
            async with conn.begin():
                progress = await _ensure_progress(conn, op_id, definition, worker)
                if progress["state"] == "complete":
                    recount_sql = f"SELECT count(*) FROM {{table_name}} WHERE {{column}} IS NULL"
                    pending = int((await conn.execute(text(recount_sql))).scalar() or 0)
                    if pending == 0:
                        note = "already complete: zero pending rows re-counted"
                        report.update(state="complete", rows_processed=progress["rows_processed"], note=note)
                        return report
                    # rows appeared after completion (an application version that does not write the column was
                    # still live): the operation is reopened from the start; nothing already filled is touched
                    reopen = (
                        f"UPDATE {{PROGRESS_TABLE}} SET state = 'running', last_key = NULL, finished_at = NULL, "
                        "updated_at = now() WHERE op_id = :op"
                    )
                    await conn.execute(text(reopen), {{"op": op_id}})
                    progress["last_key"] = None
                    report["note"] = f"{{pending}} rows appeared after completion: reopened"
                claim = (
                    f"UPDATE {{PROGRESS_TABLE}} SET state = 'running', worker = :w, updated_at = now() "
                    "WHERE op_id = :op"
                )
                await conn.execute(text(claim), {{"w": worker, "op": op_id}})
            last_key = progress["last_key"]
            passes = 0
            stopped = False
            while passes < MAX_PASSES:
                passes += 1
                while True:
                    over_batches = max_batches is not None and report["batches_this_run"] >= max_batches
                    over_time = max_seconds is not None and time.monotonic() - started >= max_seconds
                    if over_batches or over_time:
                        stopped = True
                        break
                    count, last_key = await _batch(
                        conn, table_name, column, expression, params, last_key, batch_size, op_id
                    )
                    if count == 0:
                        break
                    report["batches_this_run"] += 1
                    report["rows_this_run"] += count
                    if batch_pause_ms:
                        await asyncio.sleep(batch_pause_ms / 1000.0)
                if stopped:
                    report.update(state="running", note="stopped by the batch or time limit; resume to continue")
                    break
                report["passes_this_run"] += 1
                count_sql = f"SELECT count(*) FROM {{table_name}} WHERE {{column}} IS NULL"
                async with conn.begin():  # the count and the pass counter together: no autobegun transaction left open
                    pending = (await conn.execute(text(count_sql))).scalar() or 0
                    passes_sql = f"UPDATE {{PROGRESS_TABLE}} SET passes = passes + 1 WHERE op_id = :op"
                    await conn.execute(text(passes_sql), {{"op": op_id}})
                report["pending_rows"] = int(pending)
                if pending == 0:
                    await _set_state(conn, op_id, "complete")
                    report["state"] = "complete"
                    break
                last_key = None  # rows appeared or were skipped during the pass: another pass from the start
                report["state"] = "running"
            else:
                note = f"{{report.get('pending_rows')}} rows still NULL after {{MAX_PASSES}} passes"
                await _set_state(conn, op_id, "incomplete", note)
                report.update(state="incomplete", note=note + ": an application that does not write the column is live")
            final_sql = f"SELECT state, rows_processed, batches, passes FROM {{PROGRESS_TABLE}} WHERE op_id = :op"
            row = (await conn.execute(text(final_sql), {{"op": op_id}})).mappings().first()
            report.update(
                rows_processed=row["rows_processed"], batches=row["batches"], passes=row["passes"], state=row["state"]
            )
    except (OperationalError, InterfaceError) as exc:  # the connection dropped after it was established
        raise BackfillError(EXIT_CONNECTION, f"database connection failed: {{exc.orig if exc.orig else exc}}") from exc
    finally:
        await engine.dispose()
    report["duration_s"] = round(time.monotonic() - started, 3)
    return report


async def status(op_id: str, url: str) -> dict:
    engine = _engine(url)
    try:
        async with _connect(engine) as conn:
            status_sql = f"SELECT * FROM {{PROGRESS_TABLE}} WHERE op_id = :op"
            row = (await conn.execute(text(status_sql), {{"op": op_id}})).mappings().first()
            return {{"operation": op_id, "progress": _jsonable(dict(row)) if row else None}}
    except (OperationalError, InterfaceError) as exc:  # the connection dropped after it was established
        raise BackfillError(EXIT_CONNECTION, f"database connection failed: {{exc.orig if exc.orig else exc}}") from exc
    finally:
        await engine.dispose()


async def _operation_evidence(
    conn: AsyncConnection, op_id: str, definition: dict, progress_exists: bool, problems: list[str]
) -> dict:
    column = (await conn.execute(text(COLUMN_STATE), {{"t": definition["table"], "c": definition["column"]}})).scalar()
    entry: dict[str, Any] = {{
        "table": definition["table"],
        "column": definition["column"],
        "state_declared": definition.get("state_declared"),
        "column_present": column is not None,
        "nullable": None if column is None else column == "YES",
        "pending_rows": None,
        "progress": None,
    }}
    if column is not None:
        pending_sql = f"SELECT count(*) FROM {{q(str(definition['table']))}} WHERE {{q(str(definition['column']))}} IS NULL"
        entry["pending_rows"] = int((await conn.execute(text(pending_sql))).scalar() or 0)
    if progress_exists:
        row = (
            await conn.execute(
                text(
                    f"SELECT state, rows_processed, batches, passes, last_key, checksum, updated_at, finished_at "
                    f"FROM {{PROGRESS_TABLE}} WHERE op_id = :op"
                ),
                {{"op": op_id}},
            )
        ).mappings().first()
        entry["progress"] = _jsonable(dict(row)) if row else None
        if row and row["checksum"] == CONTRACT_SENTINEL:
            entry["progress"]["recorded_by"] = "the contract revision: the table was empty, nothing to backfill"
        elif row and row["checksum"] != checksum_of(definition):
            problems.append(f"{{op_id}}: the checkpoint belongs to another definition")
    complete = bool(entry["progress"] and entry["progress"]["state"] == "complete") and entry["pending_rows"] == 0
    entry["backfill_complete"] = complete
    if definition.get("state_declared") == "expand" and not complete:
        state = (entry["progress"] or {{}}).get("state")
        problems.append(f"{{op_id}}: backfill not complete ({{entry['pending_rows']}} pending rows, progress {{state}})")
    if definition.get("state_declared") == "contract" and entry["nullable"]:
        problems.append(f"{{op_id}}: declared contracted but the column is still nullable")
    return entry


async def verify(url: str, application_version: str | None = None) -> dict:
    """The migration evidence: what this database holds right now for every phased change and index."""
    engine = _engine(url)
    evidence: dict[str, Any] = {{
        "schema": EVIDENCE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "expected_head": EXPECTED_HEAD,
        "application_version_declared": application_version,
        "operations": {{}},
        "indexes": [],
        "problems": [],
    }}
    problems: list[str] = evidence["problems"]
    try:
        async with _connect(engine) as conn:
            info_sql = (
                "SELECT current_database(), current_user, version(), "
                "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            )
            info = (await conn.execute(text(info_sql))).first()
            evidence["database"] = {{
                "name": info[0],
                "user": info[1],
                "server_version": info[2],
                "bypasses_rls": bool(info[3]),
            }}
            heads = [r[0] for r in (await conn.execute(text("SELECT version_num FROM alembic_version"))).all()]
            evidence["alembic"] = {{"current": heads, "matches_expected_head": heads == [EXPECTED_HEAD]}}
            if heads != [EXPECTED_HEAD]:
                problems.append(f"alembic current {{heads}} is not the expected head {{EXPECTED_HEAD}}")
            progress_exists = bool((await conn.execute(text("SELECT to_regclass(:t)"), {{"t": PROGRESS_TABLE}})).scalar())
            for op_id, definition in sorted(BACKFILLS.items()):
                entry = await _operation_evidence(conn, op_id, definition, progress_exists, problems)
                evidence["operations"][op_id] = entry
            for index in INDEXES:
                row = (await conn.execute(text(INDEX_STATE), {{"n": index["name"]}})).first()
                present, valid = row is not None, bool(row[0]) if row else False
                evidence["indexes"].append(
                    {{
                        "name": index["name"],
                        "table": index["table"],
                        "present": present,
                        "valid": valid,
                        "definition": str(row[1]) if row else None,
                    }}
                )
                if not present or not valid:
                    problems.append(f"index {{index['name']}}: {{'missing' if not present else 'invalid'}}")
    except (OperationalError, InterfaceError) as exc:  # the connection dropped after it was established
        raise BackfillError(EXIT_CONNECTION, f"database connection failed: {{exc.orig if exc.orig else exc}}") from exc
    finally:
        await engine.dispose()
    evidence["ok"] = not problems
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m {pkg}.ops.backfill")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="fill NULL values in keyset batches; resumable")
    run_p.add_argument("op_id")
    run_p.add_argument("--batch-size", type=int, default=500)
    run_p.add_argument("--max-batches", type=int, default=None)
    run_p.add_argument("--max-seconds", type=float, default=None)
    run_p.add_argument("--batch-pause-ms", type=int, default=0)
    status_p = sub.add_parser("status", help="print the checkpoint of an operation")
    status_p.add_argument("op_id")
    verify_p = sub.add_parser("verify", help="write the migration evidence for this database")
    verify_p.add_argument("--application-version", default=None, help="contract digest of the deployed application")
    verify_p.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    try:
        url = database_url()
        if args.command == "run":
            report = asyncio.run(
                run_backfill(
                    args.op_id,
                    url,
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    max_seconds=args.max_seconds,
                    batch_pause_ms=args.batch_pause_ms,
                )
            )
            code = EXIT_INCOMPLETE if report.get("state") == "incomplete" else EXIT_OK
        elif args.command == "status":
            report = asyncio.run(status(args.op_id, url))
            code = EXIT_OK
        else:
            report = asyncio.run(verify(url, args.application_version))
            code = EXIT_OK
            if args.output:
                with open(args.output, "w", encoding="utf-8") as handle:
                    json.dump(report, handle, indent=2, sort_keys=True)
                    handle.write("\\n")
    except BackfillError as exc:
        sys.stderr.write(f"backfill: {{exc}}\\n")
        return exc.code
    except (OperationalError, InterfaceError) as exc:
        sys.stderr.write(f"backfill: database connection failed: {{exc.orig if exc.orig else exc}}\\n")
        return EXIT_CONNECTION
    except SQLAlchemyError as exc:
        sys.stderr.write(f"backfill: database error: {{exc}}\\n")
        return EXIT_UNKNOWN
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str) + "\\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
'''


def _wrap(text: str, width: int) -> List[str]:
    parts: List[str] = []
    while len(text) > width:
        cut = text.rfind(" ", 0, width)
        cut = cut if cut > 0 else width
        parts.append(text[: cut + 1])
        text = text[cut + 1 :]
    parts.append(text)
    return parts


def _sql_literal(field_type: str, name: str) -> str:
    samples = {"str": f"'{name}-sample'", "int": "1", "float": "1.5", "bool": "true", "uuid": "gen_random_uuid()", "datetime": "now()", "date": "current_date", "decimal": "9.99"}
    return samples.get(field_type, "'x'")


def backfill_tests(spec: Spec, changes: Sequence[Dict[str, object]], indexes: Sequence[Dict[str, object]]) -> str:
    """``tests/test_backfill.py`` of the generated project: the runner on an empty database, on rows inserted with
    NULL values in small batches, resumed from a partial run, idempotent, refusing a foreign checkpoint; the evidence."""
    pkg = spec.package
    pending = [c for c in changes if c.get("state_declared") == PHASE_EXPAND]
    target = pending[0] if pending else None  # once every change is contracted the insert-based tests skip: the columns are NOT NULL
    insert_sql = "SELECT 1"
    if target is not None:
        entity = next((e for _m, e in _ordered_entities(spec) if e.plural == target["table"]), None)
        if entity is not None:
            cols, vals = ["id"], ["gen_random_uuid()"]
            for f in entity.fields:
                if f.name == target["column"] or not f.required or f.has_default or f.references:
                    continue
                cols.append(f'"{f.name}"')
                vals.append(_sql_literal(f.type, f.name))
            insert_sql = f'INSERT INTO "{target["table"]}" ({", ".join(cols)}) VALUES ({", ".join(vals)})'
    op_literal = json.dumps(str(target["op_id"])) if target else "None"
    pending_literal = json.dumps([str(c["op_id"]) for c in pending])
    table_literal = json.dumps(str(target["table"])) if target else "None"
    column_literal = json.dumps(str(target["column"])) if target else "None"
    lines = [
        '"""Phased migrations: the backfill runner and the migration evidence, against the migrated database."""',
        "",
        "import pytest",
        "from sqlalchemy import text",
        "from sqlalchemy.ext.asyncio import create_async_engine",
        "",
        f"from {pkg}.ops import backfill",
        "from tests.conftest import run",
        "",
        f"OPERATION = {op_literal}",
        f"PENDING_OPERATIONS = {pending_literal}",
        f"PENDING_TABLE = {table_literal}",
        f"PENDING_COLUMN = {column_literal}",
        f"INDEXES = {json.dumps([index_name(str(i['table']), list(i['columns'])) for i in indexes])}",
        "INSERT_ROW = (",
        *[f"    {json.dumps(part)}" for part in _wrap(insert_sql, 100)],
        ")",
        f'TAMPER = "UPDATE {progress_table(spec)} SET checksum = \'tampered\', state = \'running\' WHERE op_id = :op"',
        "pending_only = pytest.mark.skipif(OPERATION is None, reason=\"no pending phased change\")",
        "",
        "",
        "async def _execute(url: str, sql: str, params: dict | None = None):",
        "    engine = create_async_engine(url)",
        "    try:",
        "        async with engine.begin() as conn:",
        "            result = await conn.execute(text(sql), params or {})",
        "            return result.scalar() if result.returns_rows else result.rowcount",
        "    finally:",
        "        await engine.dispose()",
        "",
        "",
        "def _pending(url: str) -> int:",
        "    return int(run(_execute(url, f'SELECT count(*) FROM \"{PENDING_TABLE}\" WHERE \"{PENDING_COLUMN}\" IS NULL')))",
        "",
        "",
        "def _insert(url: str, rows: int) -> None:",
        "    for _ in range(rows):",
        "        run(_execute(url, INSERT_ROW))",
        "",
        "",
        "@pending_only",
        "def test_backfill_completes_on_an_empty_database(migrated: str):",
        "    for operation in PENDING_OPERATIONS:",
        "        report = run(backfill.run_backfill(operation, migrated, batch_size=100))",
        '        assert report["state"] == "complete", operation',
        '        assert report["rows_this_run"] == 0',
        "",
        "",
        "@pending_only",
        "def test_backfill_fills_existing_rows_in_batches_and_is_idempotent(migrated: str):",
        "    _insert(migrated, 7)",
        "    assert _pending(migrated) == 7",
        "    first = run(backfill.run_backfill(OPERATION, migrated, batch_size=3))",
        '    assert (first["state"], first["rows_this_run"], first["batches_this_run"]) == ("complete", 7, 3)',
        "    assert _pending(migrated) == 0",
        "    again = run(backfill.run_backfill(OPERATION, migrated, batch_size=3))",
        '    assert (again["state"], again["rows_this_run"]) == ("complete", 0)',
        '    assert again["rows_processed"] == 7, "a second run processes nothing: no duplication"',
        "",
        "",
        "@pending_only",
        "def test_a_partial_run_resumes_from_its_checkpoint_without_duplication(migrated: str):",
        "    _insert(migrated, 10)",
        "    partial = run(backfill.run_backfill(OPERATION, migrated, batch_size=4, max_batches=1))",
        '    assert (partial["state"], partial["rows_this_run"]) == ("running", 4)',
        "    assert _pending(migrated) == 6",
        "    resumed = run(backfill.run_backfill(OPERATION, migrated, batch_size=4))",
        '    assert (resumed["state"], resumed["rows_this_run"]) == ("complete", 6)',
        '    assert resumed["rows_processed"] == 10',
        "",
        "",
        "@pending_only",
        "def test_a_checkpoint_of_another_definition_is_refused(migrated: str):",
        "    run(backfill.run_backfill(OPERATION, migrated, batch_size=100))",
        '    run(_execute(migrated, TAMPER, {"op": OPERATION}))',
        "    with pytest.raises(backfill.BackfillError) as excinfo:",
        "        run(backfill.run_backfill(OPERATION, migrated, batch_size=100))",
        "    assert excinfo.value.code == backfill.EXIT_CHECKPOINT",
        "",
        "",
        "def test_verify_reports_the_state_of_this_database(migrated: str):",
        "    for operation in PENDING_OPERATIONS:  # every pending backfill must have run before the evidence is ok",
        "        run(backfill.run_backfill(operation, migrated, batch_size=100))",
        "    evidence = run(backfill.verify(migrated, application_version=None))",
        '    assert evidence["schema"] == backfill.EVIDENCE_SCHEMA',
        '    assert evidence["alembic"]["matches_expected_head"], evidence["alembic"]',
        "    for name in INDEXES:",
        '        assert any(i["name"] == name and i["valid"] for i in evidence["indexes"]), evidence["indexes"]',
        '    assert evidence["ok"], evidence["problems"]',
        "",
    ]
    return "\n".join(lines)


def runner_imports_for_env(spec: Spec, has_phased: bool) -> Optional[str]:
    """The import line the Alembic env needs so the progress table is part of the metadata."""
    if not has_phased:
        return None
    return f"from {spec.package}.ops.backfill import PROGRESS as _progress_table  # noqa: F401"


def phase_descriptions(spec: Spec, new_changes: Sequence[Dict[str, object]], indexes: Sequence[Dict[str, object]], contracting: Sequence[Dict[str, object]], head_before: str, expand_rev: Optional[str], index_rev: Optional[str], contract_rev: Optional[str]) -> List[Dict[str, object]]:
    """The phases of a plan with their revision, reversibility, lock profile, preconditions and evidence."""
    phases: List[Dict[str, object]] = []
    if expand_rev:
        phases.append({
            "phase": PHASE_EXPAND,
            "revision": expand_rev,
            "transactional": True,
            "reversible": True,
            "lock_profile": "ADD COLUMN nullable without default: brief ACCESS EXCLUSIVE, no table rewrite; the progress table is created once",
            "preconditions": ["alembic chain linear and at the manifest head", "historical revisions unchanged"],
            "evidence_required": [],
            "state_after": f"declared: expand; applied: when `alembic upgrade head` ran on a database; verified: `python -m {spec.package}.ops.backfill verify`",
            "columns": [f"{c['table']}.{c['column']}" for c in new_changes],
        })
    if index_rev:
        phases.append({
            "phase": PHASE_INDEX,
            "revision": index_rev,
            "transactional": False,
            "reversible": True,
            "lock_profile": "CREATE INDEX CONCURRENTLY: SHARE UPDATE EXCLUSIVE, writes continue; two table scans; waits for transactions that started before it",
            "preconditions": ["expand revision applied", "no index of the same name with another definition"],
            "evidence_required": [],
            "state_after": "an invalid index after an interruption is dropped and rebuilt by a re-run; the revision fails rather than end with an invalid index",
            "indexes": [index_name(str(i["table"]), list(i["columns"])) for i in indexes],
        })
    if contract_rev:
        phases.append({
            "phase": PHASE_CONTRACT,
            "revision": contract_rev,
            "transactional": False,
            "reversible": True,
            "lock_profile": "ADD CONSTRAINT NOT VALID and SET NOT NULL: brief ACCESS EXCLUSIVE; VALIDATE CONSTRAINT: SHARE UPDATE EXCLUSIVE during the scan, writes continue; each statement its own transaction",
            "preconditions": ["backfill complete and zero pending rows (checked in the plan from the evidence and again by the revision on the live database)", "application version that writes the column accredited by the operator", "explicit human authorization (--authorize-contract)", "indexes valid"],
            "evidence_required": [EVIDENCE_SCHEMA],
            "state_after": "declared: contract; the downgrade is DROP NOT NULL (no data lost)",
            "columns": [f"{c['table']}.{c['column']}" for c in contracting],
        })
    return phases
