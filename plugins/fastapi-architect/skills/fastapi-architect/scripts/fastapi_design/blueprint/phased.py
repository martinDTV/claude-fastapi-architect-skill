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
from typing import Dict, List, Optional, Sequence, Tuple

from ..spec import Spec
from ._helpers import _ordered_entities, _sql_default, field_from_doc

BACKFILL_STRATEGIES = ("constant", "copy_column", "uuid", "now")  # declared by hand in a contract
MIRROR_STRATEGY = "mirror_column"  # the backfill of a rename: never declared by hand, derived from rename_fields
RENAME_STRATEGIES = ("expand_contract",)
KIND_ADD, KIND_RENAME = "add", "rename"
EVIDENCE_SCHEMA = "fastapi-architect/migration-evidence/1.2"  # 1.2 (v2.2 inc. 2): the sync truth marker, the old column's state, catalog dependents, sessions  # 1.1: mismatch_rows, columns_agree, sync state
EVIDENCE_SCHEMAS = ("fastapi-architect/migration-evidence/1.0", "fastapi-architect/migration-evidence/1.1", EVIDENCE_SCHEMA)
PHASE_EXPAND = "expand"
PHASE_INDEX = "index"
PHASE_RETIRE = "retire"  # v2.2 inc. 2: the truth of a rename moves to the new column; code state C; nothing dropped
PHASE_CONTRACT = "contract"
STATE_TRANSITION_OF = {PHASE_EXPAND: "B", PHASE_RETIRE: "C"}  # the application state a rename's declared state generates
SYNC_COMMENT_PREFIX = "fastapi-architect sync"  # the function comment: `fastapi-architect sync op=<id> truth=<column>[ retired_at=<iso>]`
TOOL_SESSION_SUFFIX = ".ops.backfill"  # application_name of the runner's own connections


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


def retire_revision(head: str, operations: Sequence[str]) -> str:
    return hashlib.sha1(f"retire|{head}|{','.join(sorted(operations))}".encode("utf-8")).hexdigest()[:12]


def drop_revision(head: str, operations: Sequence[str]) -> str:
    return hashlib.sha1(f"drop|{head}|{','.join(sorted(operations))}".encode("utf-8")).hexdigest()[:12]


def application_tag(spec: Spec, phased: Sequence[Dict[str, object]]) -> str:
    """``<package>@<12 hex>``: the name every connection of a generated application announces to PostgreSQL
    (``application_name``). It identifies a *phased generation*: the declared state of every phased change, so two
    builds that map the columns differently (B and C of a rename) carry different tags, and an evolution that touches
    no phased state leaves the tag, and ``core/db.py``, alone. The migration
    evidence lists the tags connected to the database; the destructive contract of a rename refuses while a session
    of a generation that still maps the old column, or an unidentified one, is connected."""
    states = sorted((str(p.get("op_id")), str(p.get("state_declared"))) for p in phased)
    payload = json.dumps({"package": spec.package, "phased": states}, sort_keys=True, separators=(",", ":"))
    return f"{spec.package}@{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


def sync_comment(op_id: str, truth: str, retired_at: Optional[str] = None) -> str:
    """The comment the sync function carries: which column is the truth, and since when the rename is retired."""
    return f"{SYNC_COMMENT_PREFIX} op={op_id} truth={truth}" + (f" retired_at={retired_at}" if retired_at else "")


def pending_columns(pending: Sequence[Dict[str, object]]) -> frozenset:
    return frozenset((str(c["table"]), str(c["column"])) for c in pending)


def renames_of(pending: Sequence[Dict[str, object]]) -> Dict[Tuple[str, str], Dict[str, object]]:
    """``(table, new column) -> rename entry`` for the pending renames (expand declared, retire not yet)."""
    return {(str(c["table"]), str(c["column"])): c for c in pending if c.get("kind") == KIND_RENAME}


def sync_names(table: str, op_id: str) -> Tuple[str, str]:
    """The trigger and the function that keep both columns of a rename equal, named after the operation (63 bytes at most)."""
    stem = f"{table[:30]}_{op_id}"
    return f"trg_{stem}_sync", f"fn_{stem}_sync"


def sync_function_sql(table: str, old: str, new: str, op_id: str, *, truth: str, has_default: bool) -> List[str]:
    """The body of the sync function (``CREATE OR REPLACE``) as SQL lines. ``truth`` is the column that is the source
    of truth in the declared state (the old one in transition, the new one once retired)."""
    _trigger, function = sync_names(table, op_id)
    other = new if truth == old else old
    message = f"{table}: {old} and {new} set to different values in one statement"
    if has_default:
        insert_conflict = [
            f'            NEW."{other}" := NEW."{truth}";  -- a column default made both non-NULL: the truth wins',
        ]
    else:
        insert_conflict = [
            "            RAISE EXCEPTION USING ERRCODE = 'check_violation',",
            f"                MESSAGE = '{message}';",
        ]
    return [
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$",
        "BEGIN",
        "    IF TG_OP = 'INSERT' THEN",
        f'        IF NEW."{old}" IS NOT NULL AND NEW."{new}" IS NOT NULL',
        f'           AND NEW."{old}" IS DISTINCT FROM NEW."{new}" THEN',
        *insert_conflict,
        f'        ELSIF NEW."{old}" IS NOT NULL THEN',
        f'            NEW."{new}" := NEW."{old}";',
        f'        ELSIF NEW."{new}" IS NOT NULL THEN',
        f'            NEW."{old}" := NEW."{new}";',
        "        END IF;",
        f'    ELSIF NEW."{old}" IS DISTINCT FROM OLD."{old}"',
        f'          AND NEW."{new}" IS DISTINCT FROM OLD."{new}" THEN',
        f'        IF NEW."{old}" IS DISTINCT FROM NEW."{new}" THEN',
        "            RAISE EXCEPTION USING ERRCODE = 'check_violation',",
        f"                MESSAGE = '{message}';",
        "        END IF;",
        f'    ELSIF NEW."{old}" IS DISTINCT FROM OLD."{old}" THEN',
        f'        NEW."{new}" := NEW."{old}";',
        f'    ELSIF NEW."{new}" IS DISTINCT FROM OLD."{new}" THEN',
        f'        NEW."{old}" := NEW."{new}";',
        "    END IF;",
        "    RETURN NEW;",
        "END",
        "$$",
    ]


def _execute_block(lines: Sequence[str], indent: str = "    ") -> List[str]:
    return [f"{indent}op.execute(", f'{indent}    """', *[f"{indent}    {line}" for line in lines], f'{indent}    """', f"{indent})"]


def rename_sync_ddl(table: str, old: str, new: str, op_id: str, *, truth: Optional[str] = None, has_default: bool = False) -> Tuple[List[str], List[str]]:
    """``op.execute`` lines that create (up) and drop (down) the sync trigger of a rename in transition.

    A BEFORE INSERT OR UPDATE row trigger: whichever column a writer touched, the other follows inside the same
    statement and transaction. A statement that sets both names to *different* values is ambiguous and is refused
    (SQLSTATE 23514, the writer's transaction rolls back); the one exception is an INSERT where a column default made
    both non-NULL, where the declared source of truth wins. It only assigns to NEW, so it never issues another
    statement and cannot recurse; it runs with the privileges of the writer (SECURITY INVOKER) and leaves row-level
    security to the table's policies, which still check the final row. The function's comment records the operation
    and the truth column; ``verify`` reads it back."""
    trigger, function = sync_names(table, op_id)
    truth = truth or old
    body = sync_function_sql(table, old, new, op_id, truth=truth, has_default=has_default)
    create = [f'CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON "{table}"', f"FOR EACH ROW EXECUTE FUNCTION {function}()"]
    up = _execute_block(body) + _execute_block(create)
    up += ["    op.execute(", f"        {json.dumps(f'COMMENT ON FUNCTION {function}() IS ')}", f"        {json.dumps(_sql_quote(sync_comment(op_id, truth)))}", "    )"]
    down = [f"    op.execute('DROP TRIGGER IF EXISTS {trigger} ON \"{table}\"')", f'    op.execute("DROP FUNCTION IF EXISTS {function}()")']
    return up, down


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


ONLINE_NOT_NULL = [
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
    '    validated_sql = sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n")',
    '    validated = bind.execute(validated_sql, {"n": check}).scalar()',
    "    if validated is None:",
    "        op.execute(f'ALTER TABLE \"{table}\" ADD CONSTRAINT \"{check}\" CHECK (\"{column}\" IS NOT NULL) NOT VALID')",
    "        validated = False",
    "    if not validated:",
    "        op.execute(f'ALTER TABLE \"{table}\" VALIDATE CONSTRAINT \"{check}\"')",
    "    op.execute(f'ALTER TABLE \"{table}\" ALTER COLUMN \"{column}\" SET NOT NULL')",
    "    op.execute(f'ALTER TABLE \"{table}\" DROP CONSTRAINT IF EXISTS \"{check}\"')",
    "",
    "",
]


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
        *ONLINE_NOT_NULL,
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


SYNC_STATE_SQL = [
    'SYNC_STATE = sa.text("SELECT tgenabled::text FROM pg_trigger WHERE tgname = :n AND NOT tgisinternal")',
    "SYNC_COMMENT = sa.text(",
    '    "SELECT obj_description(p.oid, \'pg_proc\') FROM pg_proc p "',
    '    "WHERE p.proname = :n AND p.pronamespace = current_schema()::regnamespace"',
    ")",
]

DEPENDENTS_SQL = [
    "DEPENDENTS = sa.text(",
    '    "SELECT d.classid::regclass::text, d.deptype, c.contype::text, "',
    '    "pg_describe_object(d.classid, d.objid, d.objsubid) FROM pg_depend d "',
    '    "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "',
    '    "LEFT JOIN pg_constraint c ON d.classid = \'pg_constraint\'::regclass AND c.oid = d.objid "',
    '    "WHERE d.refclassid = \'pg_class\'::regclass AND d.refobjid = to_regclass(:t) AND a.attname = :c "',
    '    "AND d.deptype <> \'i\' ORDER BY 4"',
    ")",
    "TABLE_TRIGGERS = sa.text(",
    '    "SELECT t.tgname, p.proname, p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "',
    '    "WHERE t.tgrelid = to_regclass(:t) AND NOT t.tgisinternal ORDER BY t.tgname"',
    ")",
    "FUNCTIONS_MENTIONING = sa.text(",
    '    "SELECT n.nspname || \'.\' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "',
    '    "WHERE n.nspname NOT IN (\'pg_catalog\', \'information_schema\') AND p.prokind = \'f\' "',
    '    "AND p.prosrc ~* :column_re AND p.prosrc ~* :table_re ORDER BY 1"',
    ")",
]

MANAGED_DEPENDENT_LINES = [
    "def _managed_dependent(kind: str, contype: str | None, description: str, table: str, column: str) -> bool:",
    '    """What goes away with the column by design: its own default and its own NOT NULL constraint',
    '    (PostgreSQL 18 records NOT NULL in pg_constraint). Every other dependent (an index, a constraint, a',
    '    foreign key from another table, a view, a policy, a generated column, extended statistics, a',
    '    publication column list) is not ours."""',
    '    if kind == "pg_attrdef" and description == f"default value for column {column} of table {table}":',
    "        return True",
    '    return kind == "pg_constraint" and contype == "n"',
    "",
    "",
    "def _unmanaged_dependents(bind, table: str, column: str) -> list[str]:",
    '    rows = bind.execute(DEPENDENTS, {"t": table, "c": column}).all()',
    "    return [str(r[3]) for r in rows if not _managed_dependent(str(r[0]), r[2], str(r[3]), table, column)]",
    "",
    "",
    "def _foreign_code_naming(bind, table: str, column: str, own: set[str], reviewed: set[str]) -> list[str]:",
    '    """Trigger functions of the table and other user functions whose source names the column: plpgsql is',
    '    not dependency-tracked, so a textual match is the only signal; a reviewed function name is accepted."""',
    '    word = re.compile(rf"\\b{re.escape(column)}\\b", re.IGNORECASE)  # Python\'s word boundary, for trigger sources',
    "    found: list[str] = []",
    '    for name, function, source in bind.execute(TABLE_TRIGGERS, {"t": table}).all():',
    "        if function in own:",
    "            continue",
    "        if word.search(str(source or \"\")):",
    '            found.append(f"trigger {name} (function {function})")',
    '    params = {"column_re": rf"\\m{column}\\M", "table_re": rf"\\m{table}\\M"}  # PostgreSQL\'s word boundaries',
    "    for (qualified,) in bind.execute(FUNCTIONS_MENTIONING, params).all():",
    "        if qualified not in reviewed and qualified.rsplit(\".\", 1)[-1] not in own:",
    '            found.append(f"function {qualified}")',
    "    return found",
]


def _default_sql_of(change: Dict[str, object]) -> str:
    """The SQL literal of the old field's default (``from_field`` of a rename entry), for SET DEFAULT."""
    return _sql_default(field_from_doc(change["from_field"]))  # type: ignore[arg-type]


def _has_default(change: Dict[str, object]) -> bool:
    doc = change.get("from_field")
    return isinstance(doc, dict) and doc.get("default") is not None


def retire_migration(spec: Spec, head: str, retiring: Sequence[Dict[str, object]]) -> str:
    """The retire revision of renames: the truth moves to the new column. Online and idempotent: the sync function is
    replaced in place (``CREATE OR REPLACE``, same trigger), the new column takes the old one's NOT NULL (online) and
    default, and the function's comment records ``truth=<new> retired_at=<now>``. Nothing is dropped. The downgrade
    puts the old column back in charge (comment, DROP NOT NULL, DROP DEFAULT); no data moves either way."""
    revision = retire_revision(head, [str(c["op_id"]) for c in retiring])
    targets = ", ".join(f"{c['table']}.{c['from_column']} -> {c['column']}" for c in retiring)
    lines = _revision_header(
        f"Retire: the truth of {targets} moves to the new column.",
        "Each statement runs in its own transaction. CREATE OR REPLACE FUNCTION swaps the sync function under the same\n"
        "trigger; SET NOT NULL is proved from a validated constraint (writes continue during the scan); SET DEFAULT takes a\n"
        "brief ACCESS EXCLUSIVE lock without a rewrite. The old column stays, equal to the new one by the trigger, for the\n"
        "application versions that still map it (B). Nothing is dropped here.",
        revision,
        head,
        ["import sqlalchemy as sa", "from alembic import op"],
        spec,
    )
    lines += ["", f'PROGRESS_TABLE = "{progress_table(spec)}"', *SYNC_STATE_SQL, "", ""]
    lines += [
        "def _require_in_sync(op_id: str, table: str, old: str, new: str, trigger: str) -> None:",
        '    """The preconditions the plan checked from the evidence, checked again on the live database."""',
        "    bind = op.get_bind()",
        '    state = bind.execute(SYNC_STATE, {"n": trigger}).scalar()',
        '    if state not in ("O", "A"):',
        '        what = "missing" if state is None else "disabled"',
        '        raise RuntimeError(f"{table}: sync trigger {trigger} is {what}; the rename {op_id} cannot be retired")',
        "    mismatch = bind.execute(sa.text(f'SELECT count(*) FROM \"{table}\" WHERE \"{new}\" IS DISTINCT FROM \"{old}\"')).scalar()",
        "    if mismatch:",
        '        raise RuntimeError(f"{table}: {mismatch} row(s) where {new} differs from {old}; run the backfill {op_id} first")',
        '    progress = bind.execute(sa.text(f"SELECT state FROM {PROGRESS_TABLE} WHERE op_id = :op"), {"op": op_id}).scalar()',
        '    if progress == "complete":',
        "        return",
        "    total = bind.execute(sa.text(f'SELECT count(*) FROM \"{table}\"')).scalar()",
        "    if progress is None and total == 0:  # a fresh environment: nothing to backfill, the fact is recorded",
        "        bind.execute(",
        "            sa.text(",
        '                f"INSERT INTO {PROGRESS_TABLE} "',
        '                "(op_id, table_name, column_name, strategy, checksum, state, finished_at) "',
        "                \"VALUES (:op, :t, :c, 'mirror_column', 'empty-table-at-contract', 'complete', now()) \"",
        '                "ON CONFLICT (op_id) DO NOTHING"',
        "            ),",
        '            {"op": op_id, "t": table, "c": new},',
        "        )",
        "        return",
        '    raise RuntimeError(f"backfill {op_id} is {progress!r}, not complete; the retire phase needs a complete backfill")',
        "",
        "",
        "def _stamp(function: str, op_id: str, truth: str, retired: bool) -> None:",
        '    """The function comment is the catalog record of which column is the truth and since when."""',
        '    comment = f"{SYNC_COMMENT_PREFIX} op={op_id} truth={truth}"',
        "    if retired:",
        '        stamp = op.get_bind().execute(sa.text("SELECT now()")).scalar()',
        '        comment += f" retired_at={stamp.isoformat()}"',
        "    quoted = comment.replace(\"'\", \"''\")",
        "    op.execute(f\"COMMENT ON FUNCTION {function}() IS '{quoted}'\")",
        "",
        "",
        *ONLINE_NOT_NULL,
        "",
        "",
        "def upgrade() -> None:",
        "    with op.get_context().autocommit_block():  # one transaction per statement",
    ]
    lines.insert(lines.index('PROGRESS_TABLE = "' + progress_table(spec) + '"'), f'SYNC_COMMENT_PREFIX = "{SYNC_COMMENT_PREFIX}"')
    for c in retiring:
        table, old, new, op_id = str(c["table"]), str(c["from_column"]), str(c["column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        lines.append(f"        _require_in_sync({json.dumps(op_id)}, {json.dumps(table)}, {json.dumps(old)}, {json.dumps(new)}, {json.dumps(trigger)})")
    for c in retiring:
        table, old, new, op_id = str(c["table"]), str(c["from_column"]), str(c["column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        has_default = _has_default(c)
        lines += _execute_block(sync_function_sql(table, old, new, op_id, truth=new, has_default=has_default), "        ")
        lines.append(f"        _stamp({json.dumps(function)}, {json.dumps(op_id)}, {json.dumps(new)}, True)")
        if c.get("required", True):
            lines.append(f"        _set_not_null_online({json.dumps(table)}, {json.dumps(new)})  # the old column's NOT NULL, carried over")
        if has_default:
            lines.append(f"        op.execute('ALTER TABLE \"{table}\" ALTER COLUMN \"{new}\" SET DEFAULT {_default_sql_of(c)}')")
    lines += ["", "", "def downgrade() -> None:", "    with op.get_context().autocommit_block():"]
    for c in reversed(list(retiring)):
        table, old, new, op_id = str(c["table"]), str(c["from_column"]), str(c["column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        has_default = _has_default(c)
        if c.get("required", True):
            lines.append(f"        op.execute('ALTER TABLE \"{table}\" ALTER COLUMN \"{new}\" DROP NOT NULL')")
        if has_default:
            lines.append(f"        op.execute('ALTER TABLE \"{table}\" ALTER COLUMN \"{new}\" DROP DEFAULT')")
        lines += _execute_block(sync_function_sql(table, old, new, op_id, truth=old, has_default=has_default), "        ")
        lines.append(f"        _stamp({json.dumps(function)}, {json.dumps(op_id)}, {json.dumps(old)}, False)")
    return "\n".join(lines) + "\n"


def retire_migration_file(head: str, retiring: Sequence[Dict[str, object]]) -> str:
    slug = "_".join(f"{c['table']}_{c['from_column']}" for c in retiring)
    return f"migrations/versions/{retire_revision(head, [str(c['op_id']) for c in retiring])}_retire_{slug[:50]}.py"


def drop_migration(spec: Spec, head: str, dropping: Sequence[Dict[str, object]], column_sql: Dict[str, str], *, approved_by: str, phrase: str, reviewed_functions: Sequence[str] = ()) -> str:
    """The destructive contract of retired renames: one transaction that locks the table, re-checks every safety
    condition on the live catalog (both columns present, NOT NULL carried, the truth marker, the trigger enabled, zero
    differing rows, no dependent object other than the column's own default and NOT NULL, no foreign code naming the
    column, backfill complete), then drops the trigger, the function and the old column with ``RESTRICT``. A failure
    anywhere rolls the whole revision back. ``column_sql`` maps the operation id to the ``sa.Column(...)`` text of the
    old column, for the downgrade: it recreates the column, copies the *current* values of the new column back, restores
    NOT NULL and the default, and recreates the sync function and trigger (truth = the new column). That is a
    compatibility restoration for B, not the recovery of the historical content at the time of the drop."""
    revision = drop_revision(head, [str(c["op_id"]) for c in dropping])
    targets = ", ".join(f"{c['table']}.{c['from_column']}" for c in dropping)
    lines = _revision_header(
        f"Contract (destructive): drop {targets}.",
        "IRREVERSIBLE for the data of the old column(s): the downgrade recreates them from the new column's current values,\n"
        "offline, and cannot restore what the old column held at the time of the drop (keep a backup if that matters).\n"
        "One transaction: ACCESS EXCLUSIVE on the table (lock_timeout 5s, fail fast), every precondition re-checked on the\n"
        "live catalog, DROP TRIGGER, DROP FUNCTION, DROP COLUMN ... RESTRICT (never CASCADE: a dependent object the checks\n"
        f"missed makes the statement fail and the transaction roll back).\nApproved by: {approved_by}\nConfirmation phrase: {phrase}",
        revision,
        head,
        ["import re", "", "import sqlalchemy as sa", "from alembic import op"],
        spec,
    )
    lines += [
        "",
        f'PROGRESS_TABLE = "{progress_table(spec)}"',
        f'SYNC_COMMENT_PREFIX = "{SYNC_COMMENT_PREFIX}"',
        'LOCK_TIMEOUT = "5s"',
        f"REVIEWED_FUNCTIONS = {json.dumps(sorted(set(reviewed_functions)))}",
        "COLUMNS = sa.text(",
        '    "SELECT column_name, is_nullable FROM information_schema.columns "',
        '    "WHERE table_name = :t AND table_schema = current_schema() AND column_name IN (:o, :n)"',
        ")",
        *SYNC_STATE_SQL,
        *DEPENDENTS_SQL,
        "",
        "",
        *MANAGED_DEPENDENT_LINES,
        "",
        "",
        "def _require_safe(",
        "    op_id: str, table: str, old: str, new: str, required: bool, trigger: str, function: str",
        ") -> None:",
        '    """Under ACCESS EXCLUSIVE: no writer can change the pair between these checks and the drop."""',
        "    bind = op.get_bind()",
        "    bind.execute(sa.text(f\"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'\"))",
        "    bind.execute(sa.text(f'LOCK TABLE \"{table}\" IN ACCESS EXCLUSIVE MODE'))",
        '    present = {str(r[0]): str(r[1]) for r in bind.execute(COLUMNS, {"t": table, "o": old, "n": new}).all()}',
        "    if old not in present or new not in present:",
        '        raise RuntimeError(f"{table}: expected both {old} and {new}; found {sorted(present)}: resolve by hand")',
        '    if required and present[new] != "NO":',
        '        raise RuntimeError(f"{table}.{new} is nullable: the retire revision did not carry the NOT NULL of {old}")',
        '    comment = str(bind.execute(SYNC_COMMENT, {"n": function}).scalar() or "")',
        '    if f"{SYNC_COMMENT_PREFIX} op={op_id} truth={new}" not in comment:',
        '        raise RuntimeError(f"{table}: {function} does not record truth={new} ({comment!r}); retire first")',
        '    state = bind.execute(SYNC_STATE, {"n": trigger}).scalar()',
        '    if state not in ("O", "A"):',
        "        what = 'missing' if state is None else 'disabled'",
        '        raise RuntimeError(f"{table}: sync trigger {trigger} is {what}: the pair may differ")',
        "    differ = sa.text(f'SELECT count(*) FROM \"{table}\" WHERE \"{new}\" IS DISTINCT FROM \"{old}\"')",
        "    mismatch = bind.execute(differ).scalar()",
        "    if mismatch:",
        '        raise RuntimeError(f"{table}: {mismatch} row(s) where {new} differs from {old}; nothing is dropped")',
        "    unmanaged = _unmanaged_dependents(bind, table, old)",
        "    if unmanaged:",
        '        raise RuntimeError(f"{table}.{old} has dependents the tool does not manage: {unmanaged}; remove them first")',
        "    foreign = _foreign_code_naming(bind, table, old, {function}, set(REVIEWED_FUNCTIONS))",
        "    if foreign:",
        '        raise RuntimeError(f"{table}.{old} is named by code the tool does not manage: {foreign}; review it first")',
        '    progress = bind.execute(sa.text(f"SELECT state FROM {PROGRESS_TABLE} WHERE op_id = :op"), {"op": op_id}).scalar()',
        '    if progress != "complete":',
        '        raise RuntimeError(f"backfill {op_id} is {progress!r}, not complete")',
        "",
        "",
        "def upgrade() -> None:",
    ]
    for c in dropping:
        table, old, new, op_id = str(c["table"]), str(c["from_column"]), str(c["column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        lines += [
            "    _require_safe(",
            f"        {json.dumps(op_id)}, {json.dumps(table)}, {json.dumps(old)}, {json.dumps(new)}, {bool(c.get('required', True))},",
            f"        {json.dumps(trigger)}, {json.dumps(function)},",
            "    )",
        ]
    for c in dropping:
        table, old, op_id = str(c["table"]), str(c["from_column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        lines.append(f"    op.execute('DROP TRIGGER IF EXISTS {trigger} ON \"{table}\"')")
        lines.append(f'    op.execute("DROP FUNCTION IF EXISTS {function}()")')
        lines.append(f"    op.execute('ALTER TABLE \"{table}\" DROP COLUMN \"{old}\" RESTRICT')  # never CASCADE")
    lines += [
        "",
        "",
        "def downgrade() -> None:",
        '    """Conditional: recreate the column, copy the current values of the new column back, restore the sync trigger.',
        '    Offline by nature (a full-table UPDATE in one transaction); the historical content of the dropped column is',
        '    not recoverable from here. Not an online rollback: stop the application, downgrade, redeploy."""',
    ]
    for c in reversed(list(dropping)):
        table, old, new, op_id = str(c["table"]), str(c["from_column"]), str(c["column"]), str(c["op_id"])
        trigger, function = sync_names(table, op_id)
        has_default = _has_default(c)
        lines.append(f"    op.add_column({json.dumps(table)}, {column_sql[op_id]})")
        if has_default:
            lines.append(f"    op.execute('ALTER TABLE \"{table}\" ALTER COLUMN \"{old}\" SET DEFAULT {_default_sql_of(c)}')")
        lines.append(f"    op.execute('UPDATE \"{table}\" SET \"{old}\" = \"{new}\"')  # the current values, not the historical ones")
        if c.get("required", True):
            lines.append(f"    op.execute('ALTER TABLE \"{table}\" ALTER COLUMN \"{old}\" SET NOT NULL')")
        lines += _execute_block(sync_function_sql(table, old, new, op_id, truth=new, has_default=has_default))
        lines += _execute_block([f'CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON "{table}"', f"FOR EACH ROW EXECUTE FUNCTION {function}()"])
        lines.append('    stamp = op.get_bind().execute(sa.text("SELECT now()")).scalar().isoformat()  # the wait restarts here')
        lines.append(f'    comment = f"{SYNC_COMMENT_PREFIX} op={op_id} truth={new} retired_at={{stamp}}"')
        lines.append(f"    op.execute(f\"COMMENT ON FUNCTION {function}() IS '{{comment}}'\")")
    return "\n".join(lines) + "\n"


def drop_migration_file(head: str, dropping: Sequence[Dict[str, object]]) -> str:
    slug = "_".join(f"{c['table']}_{c['from_column']}" for c in dropping)
    return f"migrations/versions/{drop_revision(head, [str(c['op_id']) for c in dropping])}_drop_{slug[:50]}.py"


def confirmation_phrase(dropping: Sequence[Dict[str, object]]) -> str:
    """The exact text ``--confirm-irreversible`` must carry: it names every column the revision drops."""
    return "drop " + ", ".join(f"{c['table']}.{c['from_column']}" for c in dropping)


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
    keys = ("table", "column", "field_type", "strategy", "params", "state_declared", "expand_revision", "index_revision", "retire_revision", "contract_revision", "kind", "from_column", "required", "sync", "source_of_truth")
    for c in sorted(changes, key=lambda x: str(x["op_id"])):
        lines.append(f"    {json.dumps(str(c['op_id']))}: {{")
        for key in keys:
            value = c.get(key)
            if isinstance(value, dict) and value:
                lines.append(f"        {_literal(key)}: {{")
                lines.extend(f"            {_literal(k)}: {_literal(value[k])}," for k in sorted(value, key=str))
                lines.append("        },")
            else:
                lines.append(f"        {_literal(key)}: {_literal(value)},")
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
[--batch-pause-ms M]`` fills the NULL values of a column added by an expand revision, or makes the new column of a
rename equal to the old one wherever they differ (strategy ``mirror_column``), in keyset batches ordered by the
primary key, one transaction per batch that writes the rows and the checkpoint together. ``status OP_ID`` prints
the checkpoint. For a rename in transition the old column is the source of truth: the sync trigger created by the
expand revision keeps both columns equal for every write made after it, and the backfill reconciles the rows written
before it; ``complete`` means zero rows where the columns differ.
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
import re
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
TOOL_SESSION = "{pkg}{TOOL_SESSION_SUFFIX}"  # the application_name of this runner's connections
SYNC_COMMENT_PREFIX = "{SYNC_COMMENT_PREFIX}"
CONTRACT_SENTINEL = "empty-table-at-contract"  # the checksum the contract revision records for an empty table
EXIT_OK, EXIT_USAGE, EXIT_CONNECTION, EXIT_LOCKED = 0, 1, 2, 3
EXIT_CHECKPOINT, EXIT_RLS, EXIT_INCOMPLETE, EXIT_UNKNOWN = 4, 5, 6, 7
MAX_PASSES = 3
IDENTIFIER = set("abcdefghijklmnopqrstuvwxyz0123456789_")
PENDING_IDS = (
    "SELECT id FROM {{table}} WHERE {{predicate}} AND (CAST(:last AS uuid) IS NULL OR id > CAST(:last AS uuid)) "
    "ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
)
TRIGGER_STATE = "SELECT tgenabled::text FROM pg_trigger WHERE tgname = :n AND NOT tgisinternal"
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
SYNC_COMMENT = (
    "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p "
    "WHERE p.proname = :n AND p.pronamespace = current_schema()::regnamespace"
)
COLUMN_DETAIL = (
    "SELECT is_nullable, data_type, column_default FROM information_schema.columns "
    "WHERE table_name = :t AND column_name = :c AND table_schema = current_schema()"
)
DEPENDENTS = (
    "SELECT d.classid::regclass::text, d.deptype, c.contype::text, "
    "pg_describe_object(d.classid, d.objid, d.objsubid) FROM pg_depend d "
    "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
    "LEFT JOIN pg_constraint c ON d.classid = 'pg_constraint'::regclass AND c.oid = d.objid "
    "WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = to_regclass(:t) AND a.attname = :c "
    "AND d.deptype <> 'i' ORDER BY 4"
)
TABLE_TRIGGERS = (
    "SELECT t.tgname, p.proname, p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
    "WHERE t.tgrelid = to_regclass(:t) AND NOT t.tgisinternal ORDER BY t.tgname"
)
FUNCTIONS_MENTIONING = (
    "SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND p.prokind = 'f' "
    "AND p.prosrc ~* :column_re AND p.prosrc ~* :table_re ORDER BY 1"
)
SESSIONS = (
    "SELECT coalesce(application_name, ''), usename, count(*), min(backend_start) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend' "
    "GROUP BY 1, 2 ORDER BY 1, 2"
)
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
    settings = {{"server_settings": {{"application_name": TOOL_SESSION}}}}
    return create_async_engine(url, poolclass=None, connect_args=settings)


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


def pending_predicate(definition: dict, column: str) -> str:
    """Rows the operation still has to touch: NULL values for an added column; for a rename, every row whose new
    column differs from the old one (NULL included), which is also what ``verify`` reports as mismatch_rows."""
    if definition["strategy"] == "mirror_column":
        return f"{{column}} IS DISTINCT FROM {{q(str((definition.get('params') or {{}})['source']))}}"
    return f"{{column}} IS NULL"


def value_expression(definition: dict) -> tuple[str, dict]:
    strategy, params = definition["strategy"], definition.get("params") or {{}}
    if strategy == "constant":
        return ":value", {{"value": typed_value(definition["field_type"], params["value"])}}
    if strategy in ("copy_column", "mirror_column"):
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
    predicate: str,
) -> tuple[int, str | None]:
    """One transaction: select the next pending keys after the checkpoint, fill them, advance the checkpoint."""
    async with conn.begin():
        select = text(PENDING_IDS.format(table=table_name, predicate=predicate))
        ids = [row[0] for row in (await conn.execute(select, {{"last": last_key, "n": n}})).all()]
        if not ids:
            return 0, last_key
        update = text(f"UPDATE {{table_name}} SET {{column}} = {{expression}} WHERE id = ANY(:ids) AND {{predicate}}")
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
    predicate = pending_predicate(definition, column)
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
                    recount_sql = f"SELECT count(*) FROM {{table_name}} WHERE {{predicate}}"
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
                        conn, table_name, column, expression, params, last_key, batch_size, op_id, predicate
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
                count_sql = f"SELECT count(*) FROM {{table_name}} WHERE {{predicate}}"
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
    source_present = True
    if definition.get("kind") == "rename":
        source_params = {{"t": definition["table"], "c": definition["from_column"]}}
        source = (await conn.execute(text(COLUMN_STATE), source_params)).scalar()
        source_present = source is not None
    if column is not None and source_present:
        predicate = pending_predicate(definition, q(str(definition["column"])))
        pending_sql = f"SELECT count(*) FROM {{q(str(definition['table']))}} WHERE {{predicate}}"
        entry["pending_rows"] = int((await conn.execute(text(pending_sql))).scalar() or 0)
    if definition.get("kind") == "rename":
        await _rename_evidence(conn, op_id, definition, entry, problems)
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
    recorded = bool(entry["progress"] and entry["progress"]["state"] == "complete")
    complete = recorded and (entry["pending_rows"] == 0 or (entry["pending_rows"] is None and not source_present))
    entry["backfill_complete"] = complete
    if definition.get("state_declared") in ("expand", "retire") and not complete:
        state = (entry["progress"] or {{}}).get("state")
        problems.append(f"{{op_id}}: backfill not complete ({{entry['pending_rows']}} pending rows, progress {{state}})")
    if definition.get("state_declared") == "contract" and entry["nullable"] and definition.get("required", True):
        problems.append(f"{{op_id}}: declared contracted but the column is still nullable")
    return entry


def _parse_sync_comment(comment: str | None) -> dict[str, str | None]:
    """``fastapi-architect sync op=<id> truth=<column> [retired_at=<iso>]`` as written by the revisions."""
    out: dict[str, str | None] = {{"truth": None, "retired_at": None}}
    if comment and comment.startswith(SYNC_COMMENT_PREFIX):
        for token in comment[len(SYNC_COMMENT_PREFIX) :].split():
            key, _sep, value = token.partition("=")
            if key in out:
                out[key] = value
    return out


def _managed_dependent(kind: str, contype: str | None, description: str, table: str, column: str) -> bool:
    """What goes away with the column by design: its own default and its own NOT NULL constraint. Everything else
    (an index, a constraint, a foreign key from another table, a view, a policy, a generated column, extended
    statistics, a publication column list) is not the tool's and blocks a drop."""
    if kind == "pg_attrdef" and description == f"default value for column {{column}} of table {{table}}":
        return True
    return kind == "pg_constraint" and contype == "n"


async def _rename_evidence(
    conn: AsyncConnection, op_id: str, definition: dict, entry: dict, problems: list[str]
) -> None:
    """What the retire and the destructive contract of a rename need to know about the live database."""
    sync = dict(definition.get("sync") or {{}})
    table, old, new = str(definition["table"]), str(definition["from_column"]), str(definition["column"])
    declared = definition.get("state_declared")
    state = (await conn.execute(text(TRIGGER_STATE), {{"n": sync.get("trigger")}})).scalar()
    comment = (await conn.execute(text(SYNC_COMMENT), {{"n": sync.get("function")}})).scalar()
    marker = _parse_sync_comment(comment)
    columns: dict[str, dict] = {{}}
    for name in (old, new):
        row = (await conn.execute(text(COLUMN_DETAIL), {{"t": table, "c": name}})).first()
        columns[name] = {{
            "present": row is not None,
            "nullable": None if row is None else row[0] == "YES",
            "type": None if row is None else row[1],
            "default": None if row is None else row[2],
        }}
    dependents = []
    for kind, deptype, contype, description in (await conn.execute(text(DEPENDENTS), {{"t": table, "c": old}})).all():
        managed = _managed_dependent(str(kind), contype, str(description), table, old)
        dependents.append(
            {{"kind": str(kind), "deptype": str(deptype), "description": str(description), "managed": managed}}
        )
    word = re.compile(rf"\\b{{re.escape(old)}}\\b", re.IGNORECASE)  # Python's word boundary, for trigger sources
    own = {{sync.get("function")}}
    other_triggers = []
    for name, function, source in (await conn.execute(text(TABLE_TRIGGERS), {{"t": table}})).all():
        if function in own:
            continue
        mentions = bool(word.search(str(source or "")))
        other_triggers.append({{"name": str(name), "function": str(function), "mentions_column": mentions}})
    mentioning_params = {{"column_re": rf"\\m{{old}}\\M", "table_re": rf"\\m{{table}}\\M"}}  # PostgreSQL's word boundaries
    functions = [str(r[0]) for r in (await conn.execute(text(FUNCTIONS_MENTIONING), mentioning_params)).all()]
    functions = [f for f in functions if f.rsplit(".", 1)[-1] not in own]
    entry.update(
        kind="rename",
        source_column=old,
        source_of_truth=definition.get("source_of_truth"),
        source_present=columns[old]["present"],
        columns=columns,
        mismatch_rows=entry["pending_rows"],
        columns_agree=entry["pending_rows"] == 0 if entry["pending_rows"] is not None else None,
        sync={{
            "mechanism": sync.get("mechanism"),
            "trigger": sync.get("trigger"),
            "function": sync.get("function"),
            "present": state is not None,
            "enabled": state in ("O", "A"),
            "truth": marker["truth"],
            "retired_at": marker["retired_at"],
        }},
        dependents=dependents,
        unmanaged_dependents=[d["description"] for d in dependents if not d["managed"]],
        other_triggers=other_triggers,
        functions_mentioning=functions,
    )
    if declared in ("expand", "retire"):
        if state is None or state not in ("O", "A"):
            what = "missing" if state is None else "disabled"
            problems.append(f"{{op_id}}: sync trigger {{sync.get('trigger')}} is {{what}}: writes are not mirrored")
        expected_truth = old if declared == "expand" else new
        if marker["truth"] != expected_truth:
            found = marker["truth"]
            problems.append(f"{{op_id}}: sync truth={{found}}, expected {{expected_truth}} for state {{declared}}")
        if declared == "retire" and definition.get("required", True) and columns[new]["nullable"]:
            problems.append(f"{{op_id}}: declared retired but {{new}} is still nullable: retire revision not applied")
    elif declared == "contract":
        if columns[old]["present"]:
            problems.append(f"{{op_id}}: declared contracted but {{old}} is still in the table")
        if state is not None:
            problems.append(f"{{op_id}}: declared contracted but the sync trigger {{sync.get('trigger')}} still exists")


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
        "sessions": [],
        "tool_session": TOOL_SESSION,
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
            for name, user, count, oldest in (await conn.execute(text(SESSIONS))).all():
                identified = str(name) == TOOL_SESSION or str(name).startswith("{pkg}@")
                started = oldest.isoformat() if isinstance(oldest, datetime) else str(oldest)
                evidence["sessions"].append(
                    {{
                        "application_name": str(name),
                        "user": str(user),
                        "count": int(count),
                        "oldest_backend_start": started,
                        "identified": identified,
                    }}
                )
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


def _sql_literal_changed(field_type: str, name: str) -> str:
    """A second value of the type, different from ``_sql_literal`` (the trigger test writes it to either column)."""
    samples = {"str": f"'{name}-changed'", "int": "2", "float": "2.5", "bool": "false", "uuid": "'11111111-1111-4111-8111-111111111111'", "datetime": "now() - interval '1 day'", "date": "current_date - 1", "decimal": "19.99"}
    return samples.get(field_type, "'y'")


def _sql_literal_third(field_type: str, name: str) -> Optional[str]:
    """A third value of the type, different from the other two (the contradiction test sets both columns of a row that
    holds the first value to the second and the third); None when the type has no third value (bool)."""
    samples = {"str": f"'{name}-third'", "int": "3", "float": "3.5", "uuid": "'22222222-2222-4222-8222-222222222222'", "datetime": "now() - interval '2 days'", "date": "current_date - 2", "decimal": "29.99"}
    return samples.get(field_type)


def backfill_tests(spec: Spec, changes: Sequence[Dict[str, object]], indexes: Sequence[Dict[str, object]]) -> str:
    """``tests/test_backfill.py`` of the generated project: the runner on an empty database, on rows inserted with
    NULL values in small batches, resumed from a partial run, idempotent, refusing a foreign checkpoint; the evidence."""
    pkg = spec.package
    pending = [c for c in changes if c.get("state_declared") == PHASE_EXPAND]
    target = pending[0] if pending else None  # once every change is contracted the insert-based tests skip: the columns are NOT NULL
    renames = [c for c in changes if c.get("kind") == KIND_RENAME and c.get("state_declared") in (PHASE_EXPAND, PHASE_RETIRE)]  # the trigger exists until the drop
    insert_sql = "SELECT 1"
    if target is not None:
        entity = next((e for _m, e in _ordered_entities(spec) if e.plural == target["table"]), None)
        if entity is not None:
            renamed_here = {str(r["column"]) for r in renames if r["table"] == target["table"]}
            cols, vals = ["id"], ["gen_random_uuid()"]
            for f in entity.fields:
                if f.name == target["column"] or f.name in renamed_here or not f.required or f.has_default or f.references:
                    continue
                cols.append(f'"{f.name}"')
                vals.append(_sql_literal(f.type, f.name))
            for r in renames:  # the old columns still exist and keep their constraints: an old-version writer fills them
                if r["table"] == target["table"]:
                    cols.append(f'"{r["from_column"]}"')
                    vals.append(_sql_literal(str(r.get("field_type")), str(r["from_column"])))
            insert_sql = f'INSERT INTO "{target["table"]}" ({", ".join(cols)}) VALUES ({", ".join(vals)})'
    op_literal = json.dumps(str(target["op_id"])) if target else "None"
    if target is not None and target.get("kind") == KIND_RENAME:
        predicate = f'"{target["column"]}" IS DISTINCT FROM "{target["from_column"]}"'
        sync_trigger = json.dumps(sync_names(str(target["table"]), str(target["op_id"]))[0])
    else:
        predicate = f'"{target["column"]}" IS NULL' if target else "false"
        sync_trigger = "None"
    def _insert_for(table: str) -> str:
        entity_of = next((e for _m, e in _ordered_entities(spec) if e.plural == table), None)
        renamed = {str(r["column"]) for r in renames if r["table"] == table}
        cols, vals = ["id"], ["gen_random_uuid()"]
        for f in (entity_of.fields if entity_of is not None else []):
            if f.name in renamed or not f.required or f.has_default or f.references:
                continue
            cols.append(f'"{f.name}"')
            vals.append(_sql_literal(f.type, f.name))
        for r in renames:
            if r["table"] == table:
                cols.append(f'"{r["from_column"]}"')
                vals.append(_sql_literal(str(r.get("field_type")), str(r["from_column"])))
        return f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(vals)})'

    renames_literal_items = [{"op": str(r["op_id"]), "table": str(r["table"]), "old": str(r["from_column"]), "new": str(r["column"]), "sample": _sql_literal(str(r.get("field_type")), str(r["from_column"])), "changed": _sql_literal_changed(str(r.get("field_type")), str(r["from_column"])), "third": _sql_literal_third(str(r.get("field_type")), str(r["from_column"])), "insert": _insert_for(str(r["table"]))} for r in renames]
    pending_literal = json.dumps([str(c["op_id"]) for c in pending])
    table_literal = json.dumps(str(target["table"])) if target else "None"
    column_literal = json.dumps(str(target["column"])) if target else "None"
    lines = [
        '"""Phased migrations: the backfill runner and the migration evidence, against the migrated database."""',
        "",
        "import pytest",
        "from sqlalchemy import text",
        "from sqlalchemy.exc import DBAPIError",
        "from sqlalchemy.ext.asyncio import create_async_engine",
        "",
        f"from {pkg}.ops import backfill",
        "from tests.conftest import run",
        "",
        f"OPERATION = {op_literal}",
        f"PENDING_OPERATIONS = {pending_literal}",
        f"PENDING_TABLE = {table_literal}",
        f"PENDING_COLUMN = {column_literal}",
        f"PENDING_PREDICATE = {json.dumps(predicate)}",
        f"SYNC_TRIGGER = {sync_trigger}",
        "RENAMES = [",
        *[line for item in renames_literal_items for line in ["    {", *[part for k in sorted(item) for part in ([f"        {_literal(k)}: (", *[f"            {json.dumps(piece)}" for piece in _wrap(str(item[k]), 90)], "        ),"] if k == "insert" else [f"        {_literal(k)}: {_literal(item[k])},"])], "    },"]],
        "]",
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
        "    return int(run(_execute(url, f'SELECT count(*) FROM \"{PENDING_TABLE}\" WHERE {PENDING_PREDICATE}')))",
        "",
        "",
        "def _insert(url: str, rows: int) -> None:",
        "    # rows a rename's sync trigger would mirror at once are inserted with it disabled: they stand for the rows written",
        "    # before the expand revision existed, the ones the backfill is for",
        "    if SYNC_TRIGGER:",
        "        run(_execute(url, f'ALTER TABLE \"{PENDING_TABLE}\" DISABLE TRIGGER {SYNC_TRIGGER}'))",
        "    try:",
        "        for _ in range(rows):",
        "            run(_execute(url, INSERT_ROW))",
        "    finally:",
        "        if SYNC_TRIGGER:",
        "            run(_execute(url, f'ALTER TABLE \"{PENDING_TABLE}\" ENABLE TRIGGER {SYNC_TRIGGER}'))",
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
        "    for item in RENAMES:",
        '        entry = evidence["operations"][item["op"]]',
        '        assert entry["columns_agree"] and entry["mismatch_rows"] == 0 and entry["sync"]["enabled"], entry',
        "",
        "",
        '@pytest.mark.skipif(not RENAMES, reason="no rename in transition")',
        'def test_the_sync_trigger_mirrors_writes_of_either_column(migrated: str):',
        '    for item in RENAMES:',
        '        table, old, new = item["table"], item["old"], item["new"]',
        '        agree = f\'SELECT "{old}" IS NOT DISTINCT FROM "{new}" FROM "{table}" WHERE id = :id\'',
        '        insert = item["insert"] + " RETURNING id"  # every required column of the table, the old names among them',
        '        row = run(_execute(migrated, insert))  # an old-version insert: the new column is mirrored at once',
        '        assert run(_execute(migrated, agree, {"id": row}))',
        '        run(_execute(migrated, f\'UPDATE "{table}" SET "{new}" = {item["changed"]} WHERE id = :id\', {"id": row}))',
        '        assert run(_execute(migrated, agree, {"id": row}))  # a new-name write: the old column follows',
        '        run(_execute(migrated, f\'UPDATE "{table}" SET "{old}" = {item["sample"]} WHERE id = :id\', {"id": row}))',
        '        assert run(_execute(migrated, agree, {"id": row}))  # an old-name write: the new column follows',
        '        run(_execute(migrated, f\'DELETE FROM "{table}" WHERE id = :id\', {"id": row}))',
        "",
        "",
        '@pytest.mark.skipif(not RENAMES, reason="no rename in transition")',
        "def test_the_sync_trigger_refuses_one_statement_that_sets_both_names_to_different_values(migrated: str):",
        '    for item in [r for r in RENAMES if r["third"] is not None]:  # a type with three distinct values',
        '        table, old, new = item["table"], item["old"], item["new"]',
        '        agree = f\'SELECT "{old}" IS NOT DISTINCT FROM "{new}" FROM "{table}" WHERE id = :id\'',
        '        row = run(_execute(migrated, item["insert"] + " RETURNING id"))  # both columns hold the sample value',
        '        contradiction = f\'UPDATE "{table}" SET "{old}" = {item["changed"]}, "{new}" = {item["third"]} WHERE id = :id\'',
        "        with pytest.raises(DBAPIError):  # SQLSTATE 23514: the writer's transaction rolls back",
        '            run(_execute(migrated, contradiction, {"id": row}))',
        '        assert run(_execute(migrated, agree, {"id": row}))  # the row is untouched',
        '        run(_execute(migrated, f\'DELETE FROM "{table}" WHERE id = :id\', {"id": row}))',
        "",
    ]
    return "\n".join(lines)


def runner_imports_for_env(spec: Spec, has_phased: bool) -> Optional[str]:
    """The import line the Alembic env needs so the progress table is part of the metadata."""
    if not has_phased:
        return None
    return f"from {spec.package}.ops.backfill import PROGRESS as _progress_table  # noqa: F401"


DROP_PRECONDITIONS = [
    "a rename operation the manifest knows, with its stable identity (contract_operation_known)",
    "retire declared in the manifest and applied on the database: the sync function records truth=<new column> (contract_rename_retired, contract_retire_applied)",
    "evidence present, valid, fresh and generated after the retire revision ran (contract_evidence_present, contract_evidence_fresh, contract_evidence_after_retire)",
    "zero rows where the columns differ (contract_columns_agree)",
    "backfill complete (contract_backfill_complete)",
    "the application version of the retired generation declared by the operator (contract_application_version_accredited)",
    "every session of the database identified as a generation that does not map the old column, the tool's own sessions and the names the contract ignores aside; the operator declares that older versions stopped (contract_deployments_identified, contract_old_versions_declared_stopped)",
    "the minimum age since the retire when the contract configures one (contract_retire_age; a wait is never proof)",
    "NOT NULL, type and default of the old column carried by the new one (contract_constraints_carried)",
    "no dependent in the catalog other than the column's own default and NOT NULL, no trigger function or user function naming the column unless reviewed (contract_dependencies_reviewed)",
    "one Alembic head, applied on the database (contract_single_head)",
    "no drift on the files the manifest records (contract_baseline_clean)",
    "explicit human authorization (--authorize-contract and authorization.contract.declared_by) (contract_authorized)",
    "the approver's identity (authorization.destructive.approved_by) (contract_approver_identified)",
    "the irreversible confirmation, distinct from the authorization: --confirm-irreversible with the exact phrase the plan prints (contract_irreversible_confirmed)",
    "no interrupted operation in the journal and no lock held by another process (contract_no_pending_operations)",
]


def phase_descriptions(spec: Spec, new_changes: Sequence[Dict[str, object]], indexes: Sequence[Dict[str, object]], contracting: Sequence[Dict[str, object]], head_before: str, expand_rev: Optional[str], index_rev: Optional[str], contract_rev: Optional[str], *, retiring: Sequence[Dict[str, object]] = (), retire_rev: Optional[str] = None, dropping: Sequence[Dict[str, object]] = (), drop_rev: Optional[str] = None) -> List[Dict[str, object]]:
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
            "renames": [{"operation": c["op_id"], "table": c["table"], "from": c["from_column"], "to": c["column"], "sync_trigger": sync_names(str(c["table"]), str(c["op_id"]))[0]} for c in new_changes if c.get("kind") == KIND_RENAME],
        })
        if any(c.get("kind") == KIND_RENAME for c in new_changes):
            phases.append({
                "phase": "transition",
                "revision": None,
                "transactional": None,
                "reversible": True,
                "lock_profile": "no DDL: the sync trigger runs inside every INSERT and UPDATE of the table (one row lock per write, the one the statement takes anyway)",
                "preconditions": ["expand revision applied (new column and sync trigger present)", "backfill of the rename complete (zero rows where the columns differ)"],
                "evidence_required": [EVIDENCE_SCHEMA],
                "source_of_truth": "the old column, until the rename is retired",
                "read_policy": "the transitional application reads the old column (validation alias on the read schema) and exposes both names, the old one marked deprecated",
                "write_policy": "the transitional application writes both columns in the same statement; the old application writes the old column only; the sync trigger mirrors either write to the other column and refuses one statement that sets both names to different values (an INSERT where a column default made both non-NULL resolves to the source of truth); the transitional application refuses a request that sends both names (422: one name per request)",
                "coexistence": "A (old name only) and B (both names) may run together on the expanded schema; C (new name only) arrives with the retire evolution, after the backfill and fresh evidence",
                "state_after": "declared: expand (rename in transition); nothing is retired or dropped by this evolution",
                "renames": [f"{c['table']}.{c['from_column']} -> {c['column']}" for c in new_changes if c.get("kind") == KIND_RENAME],
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
    if retire_rev:
        phases.append({
            "phase": PHASE_RETIRE,
            "revision": retire_rev,
            "transactional": False,
            "reversible": True,
            "lock_profile": "CREATE OR REPLACE FUNCTION under the same trigger (brief lock on the function); SET NOT NULL online (ADD CONSTRAINT NOT VALID, VALIDATE with writes continuing, SET NOT NULL, DROP CONSTRAINT); SET DEFAULT: brief ACCESS EXCLUSIVE, no rewrite; each statement its own transaction",
            "preconditions": ["sync trigger present and enabled", "backfill complete and zero rows where the columns differ (checked in the plan from the evidence and again by the revision)", "database at the manifest head", "the transitional application version declared by the operator", "authorization.contract.declared_by names who declares the retire"],
            "evidence_required": [EVIDENCE_SCHEMA],
            "source_of_truth": "the new column, from this revision on; the old column stays, equal to it by the trigger, for the versions that still map it",
            "read_policy": "C (new name only) reads the new column; B keeps reading the old one, which the trigger keeps equal",
            "write_policy": "C writes the new column and the trigger fills the old one; B writes both; A's writes to the old column are still mirrored; one statement that sets both names to different values is refused",
            "coexistence": "B and C may run together on the retire schema; A keeps working through the trigger but is outside this phase's contract and must be gone before the destructive contract; the application tags its connections (application_name) so the evidence can tell the generations apart",
            "state_after": "declared: retire; the function comment records truth=<new column> and retired_at; downgrade: DROP NOT NULL, DROP DEFAULT and the comment back to the old column (no data moves)",
            "renames": [f"{c['table']}.{c['from_column']} -> {c['column']}" for c in retiring],
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
    if drop_rev:
        phases.append({
            "phase": PHASE_CONTRACT,
            "revision": drop_rev,
            "destructive": True,
            "transactional": True,
            "reversible": "conditional",
            "lock_profile": "one transaction: ACCESS EXCLUSIVE on the table (lock_timeout 5s: it fails rather than queue behind traffic), every precondition re-checked on the live catalog, DROP TRIGGER, DROP FUNCTION, DROP COLUMN ... RESTRICT (never CASCADE); a failure anywhere rolls everything back",
            "preconditions": list(DROP_PRECONDITIONS),
            "evidence_required": [EVIDENCE_SCHEMA],
            "ddl_reversibility": "conditional: the downgrade recreates the old column, copies the current values of the new column back, restores NOT NULL and the default, and recreates the sync trigger (truth = the new column); offline, a full-table UPDATE in one transaction",
            "data_recovery": "not provided: the content the old column had at the time of the drop is not recoverable from the database once dropped; keep a backup taken before the upgrade if it matters (after the checks it equals the new column, which stays)",
            "compatibility_restoration": "B works again after the downgrade (both columns, trigger); there is no online rollback: stop, downgrade, redeploy",
            "state_after": "declared: contract; the old column, its sync trigger and function are gone; the operation stays in the runner's definitions for the evidence",
            "columns": [f"{c['table']}.{c['from_column']}" for c in dropping],
        })
    return phases
