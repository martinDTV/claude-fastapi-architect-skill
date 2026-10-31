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

from .blueprint.phased import BACKFILL_STRATEGIES, EVIDENCE_SCHEMA
from .spec import IDENT_RE

DEFAULT_EVIDENCE_MAX_AGE_HOURS = 24.0
OP_ID_RE = re.compile(r"^[0-9a-f]{16}$")
PHASE_STATES = ("expand", "contract")

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
        if key != "contract" and not str(key).startswith("x-"):
            errors.append(f"evolution.authorization.{key}: unknown property (known: contract)")
    contract = raw.get("contract")
    if contract is None:
        return {}, errors
    if not isinstance(contract, dict):
        return {}, errors + ["evolution.authorization.contract: expected an object with declared_by, evidence, max_evidence_age_hours"]
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
    return {"contract": {"declared_by": declared_by if isinstance(declared_by, str) else None, "evidence": evidence, "max_evidence_age_hours": float(age)}}, errors


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


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
    if data.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"evidence.schema: expected {EVIDENCE_SCHEMA!r}, got {data.get('schema')!r}")
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
    invalid = [i.get("name") for i in related if not i.get("valid")]
    expected_names = [i.get("name") for i in indexes if i.get("table") == change.get("table")]
    missing = [n for n in expected_names if n not in {i.get("name") for i in related}]
    out.append(("contract_indexes_valid", not invalid and not missing, "indexes of the table valid" if not (invalid or missing) else f"invalid: {invalid}, missing: {missing}"))
    return out
