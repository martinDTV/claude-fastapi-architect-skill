"""Alembic migrations: operations that deserve review before they run in production.

Nothing is executed. A destructive operation is reported as a change that
needs review with its context, not as a defect.
"""

from __future__ import annotations

import ast
import re
from typing import List, Optional, Tuple

from ..model import ENV_MIGRATION, Finding, new_finding
from ..resolver import FunctionInfo, constant_str, dotted_name
from . import FileContext
from .async_rules import iter_function_calls

CATEGORY = "migrations"

DDL_OPS = frozenset(
    {
        "create_table", "drop_table", "rename_table", "add_column", "drop_column", "alter_column",
        "create_index", "drop_index", "create_foreign_key", "create_unique_constraint",
        "create_primary_key", "create_check_constraint", "drop_constraint", "create_exclude_constraint",
    }
)
DML_PREFIXES = ("INSERT", "UPDATE", "DELETE", "MERGE", "COPY")
RAW_DDL_PREFIXES = ("ALTER", "LOCK", "VACUUM", "CLUSTER", "REINDEX", "DROP", "TRUNCATE")


def _sql_text(call: ast.Call) -> Tuple[Optional[str], bool]:
    """(sql, dynamic) for op.execute(...): unwraps sa.text("..."); dynamic when not a constant."""
    if not call.args:
        return None, False
    arg = call.args[0]
    if isinstance(arg, ast.Call) and arg.args:  # text("...") / sa.text("...")
        arg = arg.args[0]
    sql = constant_str(arg)
    if sql is not None:
        return sql, False
    return None, True


def _op_calls(func: FunctionInfo) -> List[Tuple[ast.Call, str, str]]:
    """(call, op_name, confidence) for calls on the alembic ``op`` object inside ``func``."""
    found = []
    for call in iter_function_calls(func):
        if not isinstance(call.func, ast.Attribute):
            continue
        parts = dotted_name(call.func)
        if not parts or len(parts) != 2:
            continue
        dotted = func.resolve(parts)
        if dotted == f"alembic.op.{parts[1]}":
            found.append((call, parts[1], "high"))
        elif parts[0] == "op" and dotted is None:
            found.append((call, parts[1], "medium"))  # star import or unresolved alias
    return found


def _kw(call: ast.Call, name: str) -> Optional[ast.AST]:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_false(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_true(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _column_calls(call: ast.Call) -> List[ast.Call]:
    cols = []
    for arg in [*call.args, *[k.value for k in call.keywords]]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Call):
                parts = dotted_name(sub.func)
                if parts and parts[-1] == "Column":
                    cols.append(sub)
    return cols


def _first_arg_str(call: ast.Call) -> str:
    return (constant_str(call.args[0]) if call.args else None) or ""


SQL_TABLE_RE = re.compile(r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+(?:ONLY\s+)?\"?([\w.]+)\"?", re.I)
SQL_INDEX_RE = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\"?[\w.]+\"?\s+ON\s+(?:ONLY\s+)?\"?([\w.]+)\"?", re.I)


def _table_arg(call: ast.Call, position: int, keyword: str) -> str:
    kw = _kw(call, keyword)
    if kw is not None:
        return constant_str(kw) or ""
    if len(call.args) > position:
        return constant_str(call.args[position]) or ""
    return ""


def _bulk_insert_table(call: ast.Call, table_vars: dict) -> str:
    if not call.args:
        return ""
    target = call.args[0]
    if isinstance(target, ast.Call) and target.args:
        return constant_str(target.args[0]) or ""
    if isinstance(target, ast.Name):
        return table_vars.get(target.id, "")
    return ""


def _table_variables(func: FunctionInfo) -> dict:
    """``{var: table_name}`` for ``var = sa.table("name", ...)`` / ``Table("name", ...)`` in ``func``."""
    found = {}
    for node in func.scope_nodes():
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and value.args:
                parts = dotted_name(value.func)
                if parts and parts[-1] in {"table", "Table", "create_table"}:
                    name = constant_str(value.args[0])
                    if name:
                        found[node.targets[0].id] = name
    return found


ROUTINE_DROPS = frozenset({"FUNCTION", "POLICY", "TRIGGER", "EXTENSION", "RULE"})
ROUTINE_ALTER_PREFIXES = ("ALTER SEQUENCE", "ALTER DEFAULT PRIVILEGES", "ALTER ROLE", "ALTER FUNCTION", "ALTER POLICY", "ALTER TRIGGER", "ALTER EXTENSION", "ALTER TYPE")


def _raw_verb(head: str) -> Optional[str]:
    """Normalised verb for a raw DDL statement, or None for routine statements not worth a finding."""
    h = " ".join(head.split())
    if h.startswith("ALTER TABLE") and "ROW LEVEL SECURITY" in h:
        return None  # ENABLE/FORCE RLS: metadata-only, instant
    if h.startswith(ROUTINE_ALTER_PREFIXES):
        return None
    if h.startswith("DROP"):
        words = h.split()
        obj = words[1] if len(words) > 1 else ""
        if obj in ROUTINE_DROPS:
            return None
        return f"DROP {obj}".strip()
    if h.startswith("ALTER TABLE"):
        return "ALTER TABLE"
    return h.split()[0]


def _analyse_upgrade(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    ops = _op_calls(func)
    ddl_lines: List[int] = []
    dml: List[Tuple[int, int, str]] = []
    backfill_before: List[int] = []
    created_tables: set = set()
    dynamic_reported = False
    table_vars = _table_variables(func)
    recreated_tables = {
        _table_arg(c, 1, "table_name").split(".")[-1]
        for c, n, _ in ops
        if n in {"create_unique_constraint", "create_foreign_key", "create_primary_key", "create_check_constraint", "create_exclude_constraint"}
    }
    for call, name, confidence in ops:
        line, col = call.lineno, call.col_offset
        table = _first_arg_str(call)
        label = f" on '{table}'" if table else ""
        if name == "create_table" and table:
            created_tables.add(table.split(".")[-1])
        if name == "drop_table":
            out.append(new_finding("MIG_DROP_TABLE", ctx.relpath, line, col,
                f"upgrade() drops table '{table}'. Data is lost and downgrade cannot restore it. Review that no code path still reads or writes it, that the contract phase of expand-contract is complete, and that a backup or archive exists.",
                confidence=confidence, symbol=f"op.{name}", function=func.qualname, environment=ctx.environment,
                recommended_test="Run the migration on a copy of production data and on an empty database; verify the application test suite against both."))
        elif name == "drop_column":
            col_name = constant_str(call.args[1]) if len(call.args) > 1 else None
            out.append(new_finding("MIG_DROP_COLUMN", ctx.relpath, line, col,
                f"upgrade() drops column '{col_name or '?'}'{label}. During a rolling deploy, instances still running the previous code will fail on SELECT/INSERT of that column. Deploy code that stops using it first, then drop in a later release; keep a rollback plan for the data.",
                confidence=confidence, symbol=f"op.{name}", function=func.qualname, environment=ctx.environment,
                recommended_test="Run the previous application release against the migrated schema (old code, new schema) before shipping."))
        elif name in {"drop_constraint", "drop_index"}:
            cname = table or "unnamed"
            on_table = _table_arg(call, 1, "table_name")
            where = f" on '{on_table}'" if on_table else ""
            replaced = name == "drop_constraint" and on_table.split(".")[-1] in recreated_tables
            note = " A constraint is created on the same table in this migration (replacement pattern); check the new one covers the old invariant." if replaced else ""
            out.append(new_finding("MIG_DROP_CONSTRAINT", ctx.relpath, line, col,
                f"upgrade() calls op.{name}('{cname}'){where}. Confirm which invariant or query plan relied on it and how it is restored on downgrade.{note}",
                confidence=confidence, symbol=f"op.{name}", function=func.qualname, environment=ctx.environment,
                severity="low" if (name == "drop_index" or replaced) else None))
        elif name == "add_column":
            for column in _column_calls(call):
                if _is_false(_kw(column, "nullable")) and _kw(column, "server_default") is None:
                    cname = constant_str(column.args[0]) if column.args else "?"
                    out.append(new_finding("MIG_NOT_NULL_WITHOUT_DEFAULT", ctx.relpath, column.lineno, column.col_offset,
                        f"Column '{cname}'{label} is added with nullable=False and no server_default. On a table that already has rows this fails (or rewrites the table). Add it nullable, backfill in batches, then ALTER ... SET NOT NULL in a later step.",
                        confidence=confidence, symbol="op.add_column", function=func.qualname, environment=ctx.environment,
                        recommended_test="Apply the migration to a database that contains rows in this table."))
        elif name == "alter_column":
            if _is_false(_kw(call, "nullable")):
                has_backfill = any(b < line for b in backfill_before)
                out.append(new_finding("MIG_NOT_NULL_WITHOUT_DEFAULT", ctx.relpath, line, col,
                    f"upgrade() sets NOT NULL{label} via op.alter_column. "
                    + ("A backfill (data update) appears earlier in this migration; verify it is batched and covers every row, or the ALTER fails." if has_backfill else "No backfill is visible before it in this migration; existing NULLs make the ALTER fail and the scan takes an exclusive lock."),
                    confidence="low" if has_backfill else "medium", symbol="op.alter_column", function=func.qualname, environment=ctx.environment,
                    recommended_test="Apply the migration to a database that contains NULLs in this column."))
            if _kw(call, "type_") is not None:
                out.append(new_finding("MIG_TYPE_CHANGE", ctx.relpath, line, col,
                    f"upgrade() changes a column type{label}. Depending on the conversion PostgreSQL rewrites the table under ACCESS EXCLUSIVE lock, and running code may not accept the new type. Prefer add-new-column, dual-write, backfill, switch, drop.",
                    confidence=confidence, symbol="op.alter_column", function=func.qualname, environment=ctx.environment,
                    recommended_test="Time the migration on a production-sized copy and run old and new code against the result."))
        elif name == "create_index":
            on_table = _table_arg(call, 1, "table_name").split(".")[-1]
            if on_table in created_tables:
                pass  # index on a table created in this same migration: no rows, no readers, no lock concern
            elif not _is_true(_kw(call, "postgresql_concurrently")):
                where = f" on '{on_table}'" if on_table else ""
                out.append(new_finding("MIG_INDEX_NOT_CONCURRENT", ctx.relpath, line, col,
                    f"op.create_index('{table}'){where} without postgresql_concurrently=True on an existing table. On PostgreSQL a plain CREATE INDEX blocks writes to the table for its duration. For large tables use CONCURRENTLY inside an autocommit block (op.get_context().autocommit_block()); it cannot run inside the migration transaction and must be checked for the INVALID state on failure.",
                    confidence=confidence, symbol="op.create_index", function=func.qualname, environment=ctx.environment,
                    recommended_test="Build the index on a production-sized copy while a writer runs and measure the write stall."))
        elif name == "execute":
            sql, dynamic = _sql_text(call)
            head = (sql or "").lstrip().upper()
            if dynamic:
                if not dynamic_reported:
                    dynamic_reported = True
                    out.append(new_finding("MIG_RAW_SQL", ctx.relpath, line, col,
                        "op.execute() with dynamically built SQL (reported once per migration). Review the statements' lock level, duration and reversibility explicitly; the auditor cannot read them.",
                        confidence="low", symbol="op.execute", function=func.qualname, environment=ctx.environment,
                        severity="info"))
            elif head.startswith(DML_PREFIXES):
                m = SQL_TABLE_RE.match(sql or "")
                target_table = (m.group(1).split(".")[-1] if m else "")
                if target_table and target_table in created_tables:
                    pass  # seed data into a table created in this migration
                else:
                    dml.append((line, col, head.split()[0]))
                    backfill_before.append(line)
            elif head.startswith("CREATE"):
                m = SQL_INDEX_RE.match(sql or "")
                if m and not m.group(2) and m.group(3).split(".")[-1] not in created_tables:
                    out.append(new_finding("MIG_INDEX_NOT_CONCURRENT", ctx.relpath, line, col,
                        f"Raw CREATE INDEX on '{m.group(3)}' without CONCURRENTLY on an existing table. It blocks writes for its duration; use CREATE INDEX CONCURRENTLY in an autocommit block for large tables.",
                        confidence="medium", symbol="op.execute", function=func.qualname, environment=ctx.environment,
                        recommended_test="Build the index on a production-sized copy while a writer runs and measure the write stall."))
                # other CREATE statements (extensions, functions, policies, triggers, types) are not reported
            elif head.startswith(RAW_DDL_PREFIXES):
                verb = _raw_verb(head)
                if verb is None:
                    continue  # routine statements: RLS enable/force, ALTER SEQUENCE, DROP FUNCTION/POLICY/TRIGGER/EXTENSION ...
                out.append(new_finding("MIG_RAW_SQL", ctx.relpath, line, col,
                    f"Raw '{verb}' statement in upgrade(). Review its lock level, duration and reversibility explicitly; Alembic cannot auto-generate its downgrade.",
                    confidence=confidence, symbol="op.execute", function=func.qualname, environment=ctx.environment,
                    severity="medium" if verb.startswith(("DROP TABLE", "DROP INDEX", "DROP SCHEMA", "TRUNCATE", "LOCK")) else None))
        elif name == "bulk_insert":
            target_table = _bulk_insert_table(call, table_vars).split(".")[-1]
            if target_table and target_table in created_tables:
                pass  # seeding a table created in this migration
            else:
                dml.append((line, col, "bulk_insert"))
                backfill_before.append(line)
        if name in DDL_OPS:
            ddl_lines.append(line)
    if ddl_lines and dml:
        line, col, verb = dml[0]
        out.append(new_finding("MIG_DDL_WITH_DML", ctx.relpath, line, col,
            f"This migration mixes schema changes (first at line {min(ddl_lines)}) with data changes ('{verb}' at line {line}). A large backfill inside the DDL transaction holds the schema locks for its whole duration and cannot be retried in batches. Split: expand schema in one migration, backfill in batches (or from the application) in another, contract last.",
            confidence="medium", symbol="op.execute", function=func.qualname, environment=ctx.environment,
            recommended_test="Time the migration on a production-sized copy and watch lock waits from a concurrent writer."))


def _analyse_downgrade(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    body = [s for s in func.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant))]
    noop = not body or all(isinstance(s, (ast.Pass, ast.Raise)) for s in body)
    if noop:
        how = "raises" if any(isinstance(s, ast.Raise) for s in body) else "is empty"
        out.append(new_finding("MIG_DOWNGRADE_NOOP", ctx.relpath, func.lineno, getattr(func.node, "col_offset", 0),
            f"downgrade() {how}: this revision cannot be rolled back with 'alembic downgrade'. Acceptable when documented (irreversible data change); otherwise the only recovery is roll-forward or restore.",
            confidence="high", symbol="downgrade", function=func.qualname, environment=ctx.environment))


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment != ENV_MIGRATION:
        return out
    upgrade = [f for f in ctx.functions if f.name == "upgrade" and f.parent is None]
    downgrade = [f for f in ctx.functions if f.name == "downgrade" and f.parent is None]
    for func in upgrade:
        _analyse_upgrade(ctx, func, out)
    for func in downgrade:
        _analyse_downgrade(ctx, func, out)
    return out
