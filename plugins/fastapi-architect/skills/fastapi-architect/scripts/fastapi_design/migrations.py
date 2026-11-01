"""Phased migrations on the plugin side (v2.1, increment 2): the vocabulary of the evolution contract 1.2, the
migration evidence a generated project writes, and the preconditions of a contract phase.

Three states are kept apart and never inferred from one another:

* **declared**: what the evolution contract asked for and the manifest records (``phased_changes[].state_declared``:
  ``expand`` or ``contract``); the existence of a revision file in the repository says nothing more than that;
* **applied**: what a concrete database holds (``alembic_version``, the column's nullability, the progress table);
* **verified**: what the generated ``python -m <pkg>.ops.backfill verify`` command measured on that database at a
  given time and wrote as evidence (``fastapi-architect/migration-evidence/1.0``).

The plugin never connects to a database (standard library only); it reads the evidence, checks it is fresh, is
about the right operation, the right head and the right application version, and only then lets a contract phase
be planned. The contract revision re-checks the database when it runs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .blueprint.phased import BACKFILL_STRATEGIES, EVIDENCE_SCHEMAS, RENAME_STRATEGIES
from .spec import IDENT_RE

DEFAULT_EVIDENCE_MAX_AGE_HOURS = 24.0
OP_ID_RE = re.compile(r"^[0-9a-f]{16}$")
PHASE_STATES = ("expand", "retire", "contract")  # retire (v2.2): renames only
DEFAULT_MIN_RETIRE_AGE_HOURS = 24.0  # configurable; never sufficient proof that older versions are gone
DESTRUCTIVE_KEYS = ("approved_by", "old_versions_stopped", "min_retire_age_hours", "ignore_sessions", "reviewed_functions")
PROTECTED_COLUMNS = ("id", "owner_id", "created_at")  # plus the tenant column, refused by the planner
RENAME_KEYS = ("from", "to", "strategy", "sync")

_TYPE_CHECKS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "decimal": lambda v: isinstance(v, str) and re.match(r"^-?\d+(\.\d+)?$", v) is not None,
    "uuid": lambda v: isinstance(v, str) and re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", v) is not None,
    "datetime": lambda v: isinstance(v, str) and _iso(v),
    "date": lambda v: isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v) is not None,
}


def _iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_backfill(field: Dict[str, Any], path: str) -> List[str]:
    """Errors of a ``backfill`` declaration: a known strategy, typed parameters, no SQL of any kind."""
    errors: List[str] = []
    backfill = field.get("backfill")
    if not isinstance(backfill, dict):
        return [f"{path}.backfill: expected an object with 'strategy'"]
    for key in backfill:
        if key not in ("strategy", "value", "source") and not str(key).startswith("x-"):
            errors.append(f"{path}.backfill.{key}: unknown property (known: strategy, value, source)")
    strategy = backfill.get("strategy")
    if strategy not in BACKFILL_STRATEGIES:
        errors.append(f"{path}.backfill.strategy: expected one of {', '.join(BACKFILL_STRATEGIES)}, got {strategy!r} (no SQL expressions: the runner builds every statement itself)")
        return errors
    ftype = field.get("type")
    if "default" in field:
        errors.append(f"{path}: a phased field has no 'default' (the backfill fills existing rows; new rows carry a value from the request)")
    if field.get("references"):
        errors.append(f"{path}: a reference cannot be backfilled in this version")
    if strategy == "constant":
        if "value" not in backfill:
            errors.append(f"{path}.backfill.value: required for strategy 'constant'")
        elif ftype in _TYPE_CHECKS and not _TYPE_CHECKS[ftype](backfill["value"]):
            errors.append(f"{path}.backfill.value: {backfill['value']!r} is not a literal of type {ftype!r}")
        if "source" in backfill:
            errors.append(f"{path}.backfill.source: only valid for strategy 'copy_column'")
    elif strategy == "copy_column":
        source = backfill.get("source")
        if not isinstance(source, str) or not IDENT_RE.match(source):
            errors.append(f"{path}.backfill.source: required field name (lowercase identifier) for strategy 'copy_column'")
        elif source == field.get("name"):
            errors.append(f"{path}.backfill.source: cannot copy a column onto itself")
        if "value" in backfill:
            errors.append(f"{path}.backfill.value: only valid for strategy 'constant'")
    else:
        expected = "uuid" if strategy == "uuid" else "datetime"
        if ftype != expected:
            errors.append(f"{path}.backfill.strategy: '{strategy}' needs a field of type '{expected}', got {ftype!r}")
        for key in ("value", "source"):
            if key in backfill:
                errors.append(f"{path}.backfill.{key}: not valid for strategy '{strategy}'")
    return errors


def backfill_params(backfill: Dict[str, Any]) -> Dict[str, Any]:
    strategy = backfill.get("strategy")
    if strategy == "constant":
        return {"value": backfill.get("value")}
    if strategy == "copy_column":
        return {"source": backfill.get("source")}
    return {}


def validate_indexes(raw: Any, path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list) or not raw:
        return out, [f"{path}: expected a non-empty list of {{columns, unique}} objects"]
    for i, item in enumerate(raw):
        q = f"{path}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{q}: expected an object")
            continue
        for key in item:
            if key not in ("columns", "unique") and not str(key).startswith("x-"):
                errors.append(f"{q}.{key}: unknown property (known: columns, unique)")
        columns = item.get("columns")
        if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or not IDENT_RE.match(c) for c in columns):
            errors.append(f"{q}.columns: expected a list of field names")
            continue
        if len(columns) != 1:
            errors.append(f"{q}.columns: one column per concurrent index in this version")
            continue
        if "unique" in item and not isinstance(item["unique"], bool):
            errors.append(f"{q}.unique: expected a boolean")
        out.append({"columns": list(columns), "unique": bool(item.get("unique", False))})
    return out, errors


def validate_contract_fields(raw: Any, path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    errors: List[str] = []
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list) or not raw:
        return out, [f"{path}: expected a non-empty list of {{name, operation}} objects"]
    for i, item in enumerate(raw):
        q = f"{path}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{q}: expected an object")
            continue
        for key in item:
            if key not in ("name", "operation") and not str(key).startswith("x-"):
                errors.append(f"{q}.{key}: unknown property (known: name, operation)")
        name, operation = item.get("name"), item.get("operation")
        if not isinstance(name, str) or not IDENT_RE.match(name):
            errors.append(f"{q}.name: expected a field name")
        if not isinstance(operation, str) or not OP_ID_RE.match(operation):
            errors.append(f"{q}.operation: expected the 16-hex operation id of the expand phase (see the manifest's phased_changes or the plan)")
        if isinstance(name, str) and isinstance(operation, str):
            out.append({"name": name, "operation": operation})
    return out, errors


def validate_authorization(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """``authorization.contract``: who declares the contract phase, where the evidence is, how fresh it must be."""
    errors: List[str] = []
    if raw is None:
        return {}, errors
    if not isinstance(raw, dict):
        return {}, ["evolution.authorization: expected an object"]
    for key in raw:
        if key not in ("contract", "destructive") and not str(key).startswith("x-"):
            errors.append(f"evolution.authorization.{key}: unknown property (known: contract, destructive)")
    out: Dict[str, Any] = {}
    destructive, destructive_errors = _validate_destructive(raw.get("destructive"))
    errors.extend(destructive_errors)
    if destructive:
        out["destructive"] = destructive
    contract = raw.get("contract")
    if contract is None:
        return out, errors
    if not isinstance(contract, dict):
        return out, errors + ["evolution.authorization.contract: expected an object with declared_by, evidence, max_evidence_age_hours"]
    for key in contract:
        if key not in ("declared_by", "evidence", "max_evidence_age_hours") and not str(key).startswith("x-"):
            errors.append(f"evolution.authorization.contract.{key}: unknown property")
    declared_by = contract.get("declared_by")
    if not isinstance(declared_by, str) or not declared_by.strip():
        errors.append("evolution.authorization.contract.declared_by: required (the person or process that authorises the contract phase)")
    evidence = contract.get("evidence")
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        errors.append("evolution.authorization.contract.evidence: expected the path of the migration evidence written by `python -m <pkg>.ops.backfill verify`")
    age = contract.get("max_evidence_age_hours", DEFAULT_EVIDENCE_MAX_AGE_HOURS)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age <= 0:
        errors.append("evolution.authorization.contract.max_evidence_age_hours: expected a positive number")
        age = DEFAULT_EVIDENCE_MAX_AGE_HOURS
    out["contract"] = {"declared_by": declared_by if isinstance(declared_by, str) else None, "evidence": evidence, "max_evidence_age_hours": float(age)}
    return out, errors


def _validate_destructive(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """``authorization.destructive`` (evolution 1.4): who approves a destructive contract, the explicit declaration that
    older versions stopped, the minimum age since the retire, the session names and the function names reviewed by
    hand. None of it is proof on its own; the plan needs all of it plus the evidence plus the command-line flags."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["evolution.authorization.destructive: expected an object with approved_by, old_versions_stopped, min_retire_age_hours, ignore_sessions, reviewed_functions"]
    errors: List[str] = []
    for key in raw:
        if key not in DESTRUCTIVE_KEYS and not str(key).startswith("x-"):
            errors.append(f"evolution.authorization.destructive.{key}: unknown property (known: {', '.join(DESTRUCTIVE_KEYS)})")
    approved_by = raw.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        errors.append("evolution.authorization.destructive.approved_by: required (the person who approves dropping the old column)")
    stopped = raw.get("old_versions_stopped")
    if stopped is not None and not isinstance(stopped, bool):
        errors.append("evolution.authorization.destructive.old_versions_stopped: expected true or false (the operator's explicit declaration; the evidence is still checked)")
    age = raw.get("min_retire_age_hours", DEFAULT_MIN_RETIRE_AGE_HOURS)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0:
        errors.append("evolution.authorization.destructive.min_retire_age_hours: expected a number of hours, zero or more (default 24; a wait is never proof)")
        age = DEFAULT_MIN_RETIRE_AGE_HOURS
    names: Dict[str, List[str]] = {}
    for key in ("ignore_sessions", "reviewed_functions"):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            errors.append(f"evolution.authorization.destructive.{key}: expected a list of names")
            value = []
        names[key] = [str(v) for v in value]
    return {"approved_by": approved_by if isinstance(approved_by, str) else None, "old_versions_stopped": bool(stopped) if isinstance(stopped, bool) else False, "min_retire_age_hours": float(age), "ignore_sessions": names["ignore_sessions"], "reviewed_functions": names["reviewed_functions"]}, errors


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


def validate_rename_fields(items: Any, path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Errors of a ``rename_fields`` list (evolution 1.3): identifiers, one strategy (``expand_contract``), one sync
    mechanism (``trigger``), no duplicate source or target; ``in_place`` is refused by name with the reason."""
    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list) or not items:
        return out, [f"{path}: expected a non-empty list of {{from, to, strategy}} objects"]
    seen_from: set = set()
    seen_to: set = set()
    for i, item in enumerate(items):
        p = f"{path}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{p}: expected an object")
            continue
        for key in item:
            if key not in RENAME_KEYS and not str(key).startswith("x-"):
                errors.append(f"{p}.{key}: unknown property (known: {', '.join(RENAME_KEYS)})")
        source, target = item.get("from"), item.get("to")
        if not isinstance(source, str) or not IDENT_RE.match(source):
            errors.append(f"{p}.from: expected the current field name (lowercase identifier)")
        if not isinstance(target, str) or not IDENT_RE.match(target):
            errors.append(f"{p}.to: expected the new field name (lowercase identifier)")
        if isinstance(source, str) and isinstance(target, str) and source == target:
            errors.append(f"{p}: from and to are the same name")
        strategy = item.get("strategy", "expand_contract")
        if strategy == "in_place":
            errors.append(f"{p}.strategy: 'in_place' (ALTER TABLE ... RENAME COLUMN) is not available: it is not online across application versions; this version supports 'expand_contract' only")
        elif strategy not in RENAME_STRATEGIES:
            errors.append(f"{p}.strategy: expected one of {', '.join(RENAME_STRATEGIES)}, got {strategy!r}")
        sync = item.get("sync", "trigger")
        if sync != "trigger":
            errors.append(f"{p}.sync: expected 'trigger' (the only mechanism that reflects the writes of an application version that knows only the old name), got {sync!r}")
        if isinstance(target, str) and target in PROTECTED_COLUMNS:
            errors.append(f"{p}.to: {target!r} is a column every table has; refused")
        if isinstance(source, str):
            if source in seen_from or source in seen_to:
                errors.append(f"{p}.from: {source!r} already takes part in another rename of this entity")
            seen_from.add(source)
        if isinstance(target, str):
            if target in seen_to or target in seen_from:
                errors.append(f"{p}.to: {target!r} already takes part in another rename of this entity")
            seen_to.add(target)
        if not errors or all(not e.startswith(p) for e in errors):
            out.append({"from": source, "to": target, "strategy": strategy, "sync": sync})
    return out, errors


def load_evidence(path: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Read and structurally validate a migration evidence file; nothing in it is trusted beyond its shape here."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"evidence file not found: {path}"]
    except (OSError, ValueError) as exc:
        return None, [f"evidence file unreadable: {path} ({exc})"]
    if not isinstance(data, dict):
        return None, ["evidence: expected a JSON object"]
    errors: List[str] = []
    if data.get("schema") not in EVIDENCE_SCHEMAS:
        errors.append(f"evidence.schema: expected one of {', '.join(EVIDENCE_SCHEMAS)}, got {data.get('schema')!r}")
    if not isinstance(data.get("generated_at"), str) or not _iso(data["generated_at"]):
        errors.append("evidence.generated_at: expected an ISO-8601 timestamp")
    if not isinstance(data.get("alembic"), dict) or not isinstance(data["alembic"].get("current"), list):
        errors.append("evidence.alembic.current: expected the list of applied heads")
    if not isinstance(data.get("operations"), dict):
        errors.append("evidence.operations: expected an object keyed by operation id")
    if not isinstance(data.get("indexes"), list):
        errors.append("evidence.indexes: expected a list")
    return (data if not errors else None), errors


def evidence_age(evidence: Dict[str, Any], now: Optional[datetime] = None) -> timedelta:
    generated = datetime.fromisoformat(str(evidence["generated_at"]).replace("Z", "+00:00"))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - generated


def contract_preconditions(change: Dict[str, Any], evidence: Optional[Dict[str, Any]], *, head_before: Optional[str], authorized: bool, declared_by: Optional[str], max_age_hours: float, indexes: Sequence[Dict[str, Any]] = (), now: Optional[datetime] = None) -> List[Tuple[str, bool, str]]:
    """``(name, satisfied, detail)`` for every precondition of contracting one phased change. The plan blocks on
    any unsatisfied one; the contract revision checks the database again when it runs."""
    out: List[Tuple[str, bool, str]] = []
    op_id = str(change.get("op_id"))
    if change.get("kind") == "rename":
        out.append(("contract_rename_retire_first", False, f"operation {op_id} is a rename in transition (state {change.get('state_declared')!r}): declare it in retire_fields first (the truth moves to the new column, nothing is dropped); its destructive contract is a later, separately authorised evolution"))
        return out
    out.append(("contract_authorized", bool(authorized and declared_by), f"declared by {declared_by!r} and --authorize-contract given" if authorized and declared_by else "needs --authorize-contract on the command line and authorization.contract.declared_by in the contract: a contract phase is a human decision, never the continuation of a backfill"))
    out.append(("contract_operation_pending", change.get("state_declared") == "expand", f"operation {op_id} is declared {change.get('state_declared')!r} in the manifest" if change.get("state_declared") != "expand" else f"operation {op_id}: expand declared, contract pending"))
    out.append(("contract_reversible", True, "SET NOT NULL is reversed by DROP NOT NULL; no data is dropped by this phase"))
    if evidence is None:
        out.append(("contract_evidence_present", False, "no migration evidence: run `python -m <pkg>.ops.backfill verify --application-version <digest> --output evidence.json` against the target database and name it in authorization.contract.evidence or --evidence"))
        return out
    out.append(("contract_evidence_present", True, f"evidence generated at {evidence.get('generated_at')} on database {(evidence.get('database') or {}).get('name')!r}"))
    age = evidence_age(evidence, now)
    fresh = timedelta(0) <= age <= timedelta(hours=max_age_hours)
    out.append(("contract_evidence_fresh", fresh, f"evidence is {age.total_seconds() / 3600:.1f} h old (limit {max_age_hours:g} h)" if fresh else f"evidence is stale or from the future: {age.total_seconds() / 3600:.1f} h (limit {max_age_hours:g} h); verify again"))
    current = list((evidence.get("alembic") or {}).get("current") or [])
    at_head = bool(head_before) and current == [head_before]
    out.append(("contract_database_at_head", at_head, f"database at {current}, manifest head {head_before}" + ("" if at_head else ": the expand and index revisions must be applied there first")))
    entry = (evidence.get("operations") or {}).get(op_id)
    if not isinstance(entry, dict):
        out.append(("contract_evidence_matches", False, f"the evidence says nothing about operation {op_id}"))
        return out
    same = entry.get("table") == change.get("table") and entry.get("column") == change.get("column")
    out.append(("contract_evidence_matches", same, f"evidence covers {entry.get('table')}.{entry.get('column')}" + ("" if same else f", the plan contracts {change.get('table')}.{change.get('column')}")))
    out.append(("contract_column_present", bool(entry.get("column_present")), "column present in the database" if entry.get("column_present") else "the column is not in the database: the expand revision was not applied"))
    progress = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
    complete = progress.get("state") == "complete"
    out.append(("contract_backfill_complete", complete, f"backfill state {progress.get('state')!r}, {progress.get('rows_processed')} rows in {progress.get('batches')} batches" if progress else "no progress record: the backfill never ran"))
    pending = entry.get("pending_rows")
    out.append(("contract_no_pending_rows", pending == 0, f"{pending} row(s) still NULL" if pending != 0 else "zero pending rows observed"))
    declared_version = evidence.get("application_version_declared")
    expected_version = change.get("application_version")
    accredited = bool(declared_version) and declared_version == expected_version
    out.append(("contract_application_version_accredited", accredited, f"operator declared application version {declared_version}" if accredited else f"the evidence must declare the application version that writes the column ({expected_version}); got {declared_version!r}. This is a declaration by the operator, recorded as such"))
    related = [i for i in (evidence.get("indexes") or []) if isinstance(i, dict) and i.get("table") == change.get("table")]
    invalid = [i.get("name") for i in related if i.get("present") and not i.get("valid")]
    expected_names = [i.get("name") for i in indexes if i.get("table") == change.get("table")]
    reported = {i.get("name"): i for i in related}
    missing = [n for n in expected_names if n not in reported or not reported[n].get("present")]
    out.append(("contract_indexes_valid", not invalid and not missing, "indexes of the table valid" if not (invalid or missing) else f"invalid: {invalid}, missing: {missing}"))
    return out


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _evidence_common(prefix: str, change: Dict[str, Any], evidence: Optional[Dict[str, Any]], *, head_before: Optional[str], max_age_hours: float, now: Optional[datetime]) -> Tuple[List[Tuple[str, bool, str]], Optional[Dict[str, Any]]]:
    """Presence, freshness, head and identity of the evidence entry of a rename: shared by retire and drop."""
    out: List[Tuple[str, bool, str]] = []
    op_id, table, old, new = str(change.get("op_id")), change.get("table"), change.get("from_column"), change.get("column")
    if evidence is None:
        out.append((f"{prefix}_evidence_present", False, "no migration evidence: run `python -m <pkg>.ops.backfill verify --application-version <digest> --output evidence.json` against the target database and name it in authorization.contract.evidence or --evidence"))
        return out, None
    out.append((f"{prefix}_evidence_present", True, f"evidence generated at {evidence.get('generated_at')} on database {(evidence.get('database') or {}).get('name')!r}"))
    age = evidence_age(evidence, now)
    fresh = timedelta(0) <= age <= timedelta(hours=max_age_hours)
    out.append((f"{prefix}_evidence_fresh", fresh, f"evidence is {age.total_seconds() / 3600:.1f} h old (limit {max_age_hours:g} h)" if fresh else f"evidence is stale or from the future: {age.total_seconds() / 3600:.1f} h (limit {max_age_hours:g} h); verify again"))
    current = list((evidence.get("alembic") or {}).get("current") or [])
    at_head = bool(head_before) and current == [head_before]
    out.append((f"{prefix}_database_at_head", at_head, f"database at {current}, manifest head {head_before}" + ("" if at_head else ": apply the pending revisions there first")))
    entry = (evidence.get("operations") or {}).get(op_id)
    if not isinstance(entry, dict):
        out.append((f"{prefix}_evidence_matches", False, f"the evidence says nothing about operation {op_id}"))
        return out, None
    same = entry.get("table") == table and entry.get("column") == new and entry.get("kind") == "rename" and entry.get("source_column") == old
    out.append((f"{prefix}_evidence_matches", same, f"evidence covers rename {entry.get('table')}.{entry.get('source_column')} -> {entry.get('column')}" + ("" if same else f", the plan expects {table}.{old} -> {new}")))
    return out, (entry if same else None)


def _index_health(evidence: Dict[str, Any], table: Any, indexes: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    related = [i for i in (evidence.get("indexes") or []) if isinstance(i, dict) and i.get("table") == table]
    invalid = [i.get("name") for i in related if i.get("present") and not i.get("valid")]
    expected_names = [i.get("name") for i in indexes if i.get("table") == table]
    reported = {i.get("name"): i for i in related}
    missing = [n for n in expected_names if n not in reported or not reported[n].get("present")]
    return (not invalid and not missing), ("indexes of the table valid" if not (invalid or missing) else f"invalid: {invalid}, missing: {missing}")


def retire_preconditions(change: Dict[str, Any], evidence: Optional[Dict[str, Any]], *, head_before: Optional[str], declared_by: Optional[str], max_age_hours: float, indexes: Sequence[Dict[str, Any]] = (), now: Optional[datetime] = None) -> List[Tuple[str, bool, str]]:
    """``(name, satisfied, detail)`` for moving the truth of a rename to the new column (code state C). Reversible and
    online, so no command-line flag beyond the declaring identity; every transition condition must be verified first."""
    op_id, table, old, new = str(change.get("op_id")), change.get("table"), change.get("from_column"), change.get("column")
    out: List[Tuple[str, bool, str]] = [
        ("retire_operation_known", True, f"rename {table}.{old} -> {new}, operation {op_id} (declared {change.get('state_declared')!r})"),
        ("retire_declared_by", bool(declared_by), f"declared by {declared_by!r}" if declared_by else "needs authorization.contract.declared_by: who declares that the transition is over"),
    ]
    common, entry = _evidence_common("retire", change, evidence, head_before=head_before, max_age_hours=max_age_hours, now=now)
    out.extend(common)
    if entry is None or evidence is None:
        return out
    sync = entry.get("sync") if isinstance(entry.get("sync"), dict) else {}
    live = bool(sync.get("present") and sync.get("enabled"))
    out.append(("retire_sync_enabled", live, f"sync trigger {sync.get('trigger')} present and enabled" if live else f"sync trigger {sync.get('trigger')} is {'missing' if not sync.get('present') else 'disabled'}: the pair is not maintained"))
    truth_ok = sync.get("truth") == old
    out.append(("retire_truth_is_old_column", truth_ok, f"the sync function records truth={old}" if truth_ok else f"the sync function records truth={sync.get('truth')!r}, expected {old!r} (evidence 1.1, or already retired by another path)"))
    agree = entry.get("mismatch_rows") == 0 and entry.get("columns_agree") is True
    out.append(("retire_columns_agree", agree, "zero rows where the columns differ" if agree else f"{entry.get('mismatch_rows')} row(s) where {new} differs from {old}: run the backfill, then verify again"))
    progress = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
    complete = entry.get("backfill_complete") is True or (progress.get("state") == "complete" and agree)
    out.append(("retire_backfill_complete", complete, f"backfill state {progress.get('state')!r}, {progress.get('rows_processed')} rows in {progress.get('batches')} batches" if progress else "no progress record: the backfill never ran"))
    declared_version, expected_version = evidence.get("application_version_declared"), change.get("application_version")
    accredited = bool(declared_version) and declared_version == expected_version
    out.append(("retire_application_version_accredited", accredited, f"operator declared application version {declared_version}" if accredited else f"the evidence must declare the transitional application version ({expected_version}); got {declared_version!r}"))
    out.append(("retire_indexes_valid", *_index_health(evidence, table, indexes)))
    return out


def drop_preconditions(change: Dict[str, Any], evidence: Optional[Dict[str, Any]], *, head_before: Optional[str], authorized: bool, declared_by: Optional[str], max_age_hours: float, destructive: Dict[str, Any], confirm_phrase: Optional[str], expected_phrase: str, package: str, now: Optional[datetime] = None, lock_holder: Optional[Dict[str, Any]] = None, drift_clean: bool = True, heads_on_disk: int = 1) -> List[Tuple[str, bool, str]]:
    """``(name, satisfied, detail)`` for dropping the old column of a retired rename: the sixteen requirements of the
    destructive contract, each one named. The plan blocks on any unsatisfied one; the revision re-checks the
    database-side ones under an exclusive lock when it runs."""
    now = now or datetime.now(timezone.utc)
    op_id, table, old, new = str(change.get("op_id")), change.get("table"), change.get("from_column"), change.get("column")
    destructive = destructive or {}
    approved_by = destructive.get("approved_by")
    confirmed = confirm_phrase is not None and confirm_phrase == expected_phrase
    retired = change.get("state_declared") == "retire"
    stopped = destructive.get("old_versions_stopped") is True
    out: List[Tuple[str, bool, str]] = [
        ("contract_operation_known", True, f"rename {table}.{old} -> {new}, operation {op_id}: the old column, its sync trigger and function would be dropped"),
        ("contract_rename_retired", retired, f"declared {change.get('state_declared')!r}" + ("" if retired else ": retire the rename first (retire_fields), deploy that generation, verify, then contract")),
        ("contract_authorized", bool(authorized and declared_by), f"declared by {declared_by!r} and --authorize-contract given" if authorized and declared_by else "needs --authorize-contract on the command line and authorization.contract.declared_by in the contract"),
        ("contract_approver_identified", bool(approved_by), f"approved by {approved_by!r}" if approved_by else "needs authorization.destructive.approved_by: the person who approves dropping the column"),
        ("contract_irreversible_confirmed", confirmed, f"--confirm-irreversible {expected_phrase!r} given" if confirmed else f"needs --confirm-irreversible {expected_phrase!r} on the command line (got {confirm_phrase!r}): the irreversible confirmation is distinct from --authorize-contract"),
        ("contract_old_versions_declared_stopped", stopped, "the operator declares that every version mapping the old column stopped (a declaration; the sessions are checked too)" if stopped else "needs authorization.destructive.old_versions_stopped: true, the operator's explicit declaration (never sufficient on its own)"),
        ("contract_no_pending_operations", lock_holder is None, "no interrupted operation in the journal, project lock free" if lock_holder is None else f"the project lock is held by {lock_holder.get('pid')}@{lock_holder.get('host')} ({lock_holder.get('status')}): another operation is running"),
        ("contract_baseline_clean", drift_clean, "the files the manifest records are unchanged" if drift_clean else "a managed file was edited by hand: the plan refuses before anything destructive"),
        ("contract_restrict_only", True, "DROP COLUMN ... RESTRICT in one transaction; CASCADE is never generated; a dependent the checks missed fails the statement and rolls everything back"),
        ("contract_data_recovery_declared", True, "DDL reversibility: conditional (the downgrade recreates the column from the new column's current values, offline); data recovery of the dropped content: not by this tool (a backup taken before the upgrade); compatibility restoration: downgrade, then redeploy; no online rollback"),
    ]
    common, entry = _evidence_common("contract", change, evidence, head_before=head_before, max_age_hours=max_age_hours, now=now)
    out.extend(common)
    current = list(((evidence or {}).get("alembic") or {}).get("current") or [])
    single = heads_on_disk == 1 and (evidence is None or len(current) == 1)
    out.append(("contract_single_head", single, f"one head on disk, database at {current}" if single else f"heads on disk: {heads_on_disk}, alembic_version rows: {current}"))
    if entry is None or evidence is None:
        return out
    sync = entry.get("sync") if isinstance(entry.get("sync"), dict) else {}
    retire_applied = sync.get("truth") == new and entry.get("state_declared") == "retire"
    out.append(("contract_retire_applied", retire_applied, f"the sync function records truth={new}; the evidence was written by the retired generation's runner" if retire_applied else f"the database records truth={sync.get('truth')!r} and the evidence was written by a runner declaring {entry.get('state_declared')!r}: apply the retire revision and verify with the retired generation"))
    retired_at, generated = _parse_time(sync.get("retired_at")), _parse_time(evidence.get("generated_at"))
    after = retired_at is not None and generated is not None and generated > retired_at
    out.append(("contract_evidence_after_retire", after, f"evidence generated {(generated - retired_at).total_seconds() / 3600:.1f} h after the retire" if after else f"the evidence must be generated after the retire revision ran (retired_at {sync.get('retired_at')!r}, generated_at {evidence.get('generated_at')!r})"))
    minimum = float(destructive.get("min_retire_age_hours", DEFAULT_MIN_RETIRE_AGE_HOURS))
    hours = (now - retired_at).total_seconds() / 3600 if retired_at is not None else None
    aged = hours is not None and hours >= minimum
    if aged:
        age_detail = f"retired {hours:.1f} h ago, minimum {minimum:g} h configured (a wait is never proof that older versions are gone: the sessions are checked too)"
    elif hours is not None:
        age_detail = f"retired {hours:.1f} h ago, under the minimum of {minimum:g} h (authorization.destructive.min_retire_age_hours)"
    else:
        age_detail = "retired_at unknown: the retire revision did not run on this database"
    out.append(("contract_retire_age", aged, age_detail))
    live = bool(sync.get("present") and sync.get("enabled"))
    out.append(("contract_sync_enabled", live, f"sync trigger {sync.get('trigger')} present and enabled: the pair stays equal until the drop" if live else f"sync trigger {sync.get('trigger')} is {'missing' if not sync.get('present') else 'disabled'}"))
    agree = entry.get("mismatch_rows") == 0 and entry.get("columns_agree") is True
    out.append(("contract_columns_agree", agree, "zero rows where the columns differ" if agree else f"{entry.get('mismatch_rows')} row(s) where {new} differs from {old}: nothing is dropped"))
    progress = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
    complete = entry.get("backfill_complete") is True or (progress.get("state") == "complete" and agree)
    out.append(("contract_backfill_complete", complete, f"backfill state {progress.get('state')!r}, {progress.get('rows_processed')} rows in {progress.get('batches')} batches" if progress else "no progress record: the backfill never ran"))
    declared_version, expected_version = evidence.get("application_version_declared"), change.get("application_version")
    accredited = bool(declared_version) and declared_version == expected_version
    out.append(("contract_application_version_accredited", accredited, f"operator declared application version {declared_version} (the retired generation)" if accredited else f"the evidence must declare the retired application version ({expected_version}); got {declared_version!r}"))
    columns = entry.get("columns") if isinstance(entry.get("columns"), dict) else {}
    o = columns.get(old) if isinstance(columns.get(old), dict) else {}
    n = columns.get(new) if isinstance(columns.get(new), dict) else {}
    required = bool(change.get("required", True))
    carried = bool(o.get("present") and n.get("present")) and (not required or n.get("nullable") is False) and o.get("type") == n.get("type") and ((o.get("default") is None) == (n.get("default") is None))
    detail = f"{new}: type {n.get('type')}, nullable {n.get('nullable')}, default {n.get('default')!r}; {old}: type {o.get('type')}, nullable {o.get('nullable')}, default {o.get('default')!r}"
    out.append(("contract_constraints_carried", carried, detail if carried else "the new column does not carry the old one's NOT NULL, type or default: " + detail))
    unmanaged = [str(x) for x in (entry.get("unmanaged_dependents") or [])]
    triggers = [str(t.get("name")) for t in (entry.get("other_triggers") or []) if isinstance(t, dict) and t.get("mentions_column")]
    reviewed = set(destructive.get("reviewed_functions") or [])
    functions = [str(f) for f in (entry.get("functions_mentioning") or []) if str(f) not in reviewed]
    listed = "unmanaged_dependents" in entry
    clean = listed and not unmanaged and not triggers and not functions
    if clean:
        dep_detail = "no dependent in the catalog beyond the column's own default and NOT NULL; no other trigger or user function names the column"
    elif not listed:
        dep_detail = "the evidence does not list dependents (schema 1.1): verify with the retired generation's runner"
    else:
        dep_detail = f"dependents: {unmanaged}; triggers naming the column: {triggers}; functions naming it: {functions} (catalog dependents must go by hand; a reviewed function can be named in authorization.destructive.reviewed_functions)"
    out.append(("contract_dependencies_reviewed", clean, dep_detail))
    out.append(("contract_indexes_valid", *_index_health(evidence, table, [])))
    sessions = evidence.get("sessions")
    if not isinstance(sessions, list):
        out.append(("contract_deployments_identified", False, "the evidence does not list the sessions of the database (schema 1.1): verify with the retired generation's runner"))
        return out
    compatible = {str(t) for t in (change.get("compatible_tags") or [])}
    ignored = set(destructive.get("ignore_sessions") or [])
    tool = evidence.get("tool_session")
    foreign = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        name = str(s.get("application_name") or "")
        if name in compatible or name == tool or name in ignored:
            continue
        kind = "a generation that still maps the old column" if name.startswith(f"{package}@") else "unidentified: an older version or an external client"
        foreign.append(f"{name or '<empty>'} x{s.get('count')} as {s.get('user')} ({kind})")
    if foreign:
        session_detail = f"sessions that may still map the old column: {foreign}; stop them, or name external clients in authorization.destructive.ignore_sessions"
    else:
        session_detail = f"every session is a retired generation ({sorted(compatible)}), the runner or an ignored name; {len(sessions)} group(s) observed at verify time (a snapshot: instances that connect intermittently are not seen)"
    out.append(("contract_deployments_identified", not foreign, session_detail))
    return out
