"""Safe project evolution (v2.0, increments 3 and 4): extend generated projects, and adopted hand-written ones,
under a contract.

Flow: ``discover`` the project → validate the **evolution contract** (``fastapi-architect/evolution/1.1``; 1.0 is
still read) → derive the change set → a **plan** → optionally **apply** it: first to a copy where the audit, the
route invariants, the protected files and (under the v1.9 trust model) the tests are checked, then, only if that
verification did not reject it, to the project itself through the recoverable writer (``journal``): staged
content and backups, a journal of the operation, per-file atomic renames, recovery of an interruption.

Two kinds of project:

* **generated** (a 1.1 manifest with the contract): ``add_modules`` appends modules, ``modify_modules`` adds
  optional (or defaulted) fields, operations and dependencies to existing modules. The new modules or the
  changed module are **regenerated** from the merged contract; nothing is merged by text. Files whose generated
  content does not change are never written, so manual changes on them survive; a managed integration point or
  module file with a manual change blocks. PostgreSQL projects get one additive revision (new tables and
  nullable or defaulted columns) chained after the recorded head.
* **adopted** (no manifest, an adoption baseline from ``adoption.py``): ``modify_modules`` with ``add_endpoints``
  (a handler appended to the module's router file) and ``add_fields`` (an optional field appended to an adopted
  pydantic class) through the anchored structural transforms of ``transforms.py``; only adopted, unchanged files
  can be transformed; everything else is protected.

Every change is classified ``compatible``, ``potentially_incompatible`` (refused unless a safe form is declared,
such as a default for a NOT NULL column) or ``unsupported`` (removals, renames, type changes, changes of public
contracts: refused). ``verified`` requires executed tests; ``audit_clean`` means none ran; ``inconclusive`` and
``rejected`` never write.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi_audit.engine import audit_project
from fastapi_audit.model import TOOL_VERSION
from fastapi_audit.remediate.executors import Authorization
from fastapi_audit.remediate.isolation import ExecutionPolicy, Workspace
from fastapi_audit.report import compare_reports
from fastapi_common.fs import resolve_against

from .adoption import ADOPTION
from .advisor import recommend
from .blueprint import Layout, Lineage, PhaseState, added_columns, build, delta_revision
from .blueprint.phased import (
    KIND_RENAME,
    MIRROR_STRATEGY,
    PHASE_RETIRE,
    application_tag,
    confirmation_phrase,
    contract_revision,
    drop_revision,
    index_name,
    index_revision,
    operation_id,
    phase_descriptions,
    retire_revision,
    sync_names,
)
from .discovery import ProjectModel, discover
from .generator import MANIFEST, GenerationError, WriteError, build_manifest, safe_join
from .journal import FaultInjector, Operation, pending_operations, sha256_bytes, unreadable_operations, write_recoverable
from .locking import LockHeld
from .locking import inspect as inspect_lock
from .migrations import (
    PROTECTED_COLUMNS,
    backfill_params,
    contract_preconditions,
    drop_preconditions,
    load_evidence,
    retire_preconditions,
    validate_authorization,
    validate_backfill,
    validate_contract_fields,
    validate_indexes,
    validate_rename_fields,
)
from .spec import IDENT_RE, OPERATIONS, Spec, canonical_digest, validate_spec
from .transforms import StructuralEdit, TransformError, compose, insert_class_field, insert_function
from .verify import run_generated_tests

EVOLUTION_SCHEMA = "fastapi-architect/evolution/1.4"  # 1.4 (v2.2 inc. 2): retire_fields, contract_fields on a retired rename (destructive), authorization.destructive  # 1.2 (v2.1): phased changes (backfill), concurrent indexes, contract phases, authorization; 1.3 (v2.2): rename_fields (expand_contract, in transition)
EVOLUTION_SCHEMAS = ("fastapi-architect/evolution/1.0", "fastapi-architect/evolution/1.1", "fastapi-architect/evolution/1.2", "fastapi-architect/evolution/1.3", "fastapi-architect/evolution/1.4")
EVOLUTION_SCHEMA_12 = "fastapi-architect/evolution/1.2"
EVOLUTION_SCHEMA_13 = "fastapi-architect/evolution/1.3"
EVOLUTION_SCHEMA_14 = "fastapi-architect/evolution/1.4"
PHASED_SCHEMAS = (EVOLUTION_SCHEMA_12, EVOLUTION_SCHEMA_13, EVOLUTION_SCHEMA_14)
RENAME_SCHEMAS = (EVOLUTION_SCHEMA_13, EVOLUTION_SCHEMA_14)
REPORT_SCHEMA = "evolution_report/1.5"  # 1.5 (v2.2 inc. 2): plan.migration.retires, destructive contract entries, the retire phase, contract_authorization.confirm_irreversible (additive)  # 1.4 (v2.2): plan.migration.renames and the transition phase  # 1.4 (v2.2): plan.migration.renames and the transition phase (additive)  # 1.2 (v2.1): verification.tests.run carries an `environment` record; 1.3 (v2.1 increment 2): `contract_authorization` and the phased keys of plan.migration (additive)
INVARIANTS = ("existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged")
DEFAULT_INTEGRATION_POINTS = ("pyproject.toml", "README.md", ".env.example", ".audit-fastapi.json", "src/{pkg}/main.py", "src/{pkg}/api/router.py", "src/{pkg}/core/config.py", "src/{pkg}/core/db.py", "migrations/env.py", "tests/conftest.py", "tests/test_migrations.py", "tests/test_rls.py", "tests/test_auth.py", "tests/test_jwks.py", "src/{pkg}/ops/backfill.py", "src/{pkg}/ops/backfills.py", "tests/test_backfill.py")
UNSUPPORTED_MODULE_KEYS = ("remove_fields", "rename_fields", "change_fields", "remove_operations", "remove_entities", "rename_entities", "remove_endpoints", "rename", "remove_depends_on")
STATE_PLANNED = "planned"
STATE_NOOP = "noop"
STATE_BLOCKED = "blocked"
STATE_VERIFIED = "verified"
STATE_AUDIT_CLEAN = "audit_clean"
STATE_INCONCLUSIVE = "inconclusive"
STATE_REJECTED = "rejected"
SEVERITY_BLOCKING = ("high", "critical")
COMPATIBLE, POTENTIALLY_INCOMPATIBLE, UNSUPPORTED = "compatible", "potentially_incompatible", "unsupported"
DESTRUCTIVE = "destructive"  # v2.2 inc. 2: the drop of a retired rename's old column
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class EvolutionError(RuntimeError):
    def __init__(self, source: Optional[str], errors: List[str]) -> None:
        self.source = source or "<evolution>"
        self.errors = list(errors)
        super().__init__(f"{self.source}: {len(self.errors)} contract error(s): " + "; ".join(self.errors))


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #


@dataclass
class ModuleChange:
    name: str
    add_depends_on: List[str] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)  # {name, add_fields: [...], add_operations: [...]}
    add_endpoints: List[Dict[str, Any]] = field(default_factory=list)  # adopted: {method, path, name, summary, response}
    add_fields: List[Dict[str, Any]] = field(default_factory=list)  # adopted: {class, name, type, required, default}

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "add_depends_on": list(self.add_depends_on), "entities": list(self.entities), "add_endpoints": list(self.add_endpoints), "add_fields": list(self.add_fields)}


@dataclass
class EvolutionContract:
    schema: str
    project_path: str
    package: Optional[str]
    expected_spec_digest: Optional[str]
    baseline_contract: Optional[str]
    add_modules: List[Dict[str, Any]]
    modify_modules: List[ModuleChange]
    invariants: List[str]
    allow_modify: Optional[List[str]]
    preserve: List[str]
    source: Optional[str] = None
    document: Dict[str, Any] = field(default_factory=dict)
    authorization: Dict[str, Any] = field(default_factory=dict)  # 1.2: {contract: {declared_by, evidence, max_evidence_age_hours}}

    @property
    def digest(self) -> str:
        return canonical_digest(self.document)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.schema, "project_path": self.project_path, "package": self.package, "expected_spec_digest": self.expected_spec_digest, "baseline_contract": self.baseline_contract, "add_modules": [m.get("name") for m in self.add_modules], "modify_modules": [m.to_dict() for m in self.modify_modules], "invariants": list(self.invariants), "allow_modify": self.allow_modify, "preserve": list(self.preserve), "authorization": dict(self.authorization), "digest": self.digest}


def _validate_module_changes(raw: Any, errors: List[str], schema: Optional[str] = None) -> List[ModuleChange]:
    out: List[ModuleChange] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        errors.append("evolution.modify_modules: expected a list")
        return out
    seen: set = set()
    for i, m in enumerate(raw):
        p = f"evolution.modify_modules[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{p}: expected an object")
            continue
        for key in m:
            if key in UNSUPPORTED_MODULE_KEYS:
                errors.append(f"{p}.{key}: unsupported in evolution 1.1 (removals, renames and type changes of fields, entities, endpoints or public contracts are not transformed)")
            elif key not in ("name", "add_depends_on", "entities", "add_endpoints", "add_fields") and not str(key).startswith("x-"):
                errors.append(f"{p}.{key}: unknown property")
        name = m.get("name")
        if not isinstance(name, str) or not IDENT_RE.match(name):
            errors.append(f"{p}.name: module name must be a lowercase python identifier")
            continue
        if name in seen:
            errors.append(f"{p}.name: duplicate module {name!r}")
        seen.add(name)
        deps = m.get("add_depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) or not IDENT_RE.match(d) for d in deps):
            errors.append(f"{p}.add_depends_on: expected a list of module names")
            deps = []
        entities = m.get("entities", [])
        if not isinstance(entities, list):
            errors.append(f"{p}.entities: expected a list")
            entities = []
        for j, e in enumerate(entities):
            q = f"{p}.entities[{j}]"
            if not isinstance(e, dict) or not isinstance(e.get("name"), str):
                errors.append(f"{q}: expected an object with 'name'")
                continue
            for key in e:
                if key in ("remove_fields", "change_fields", "remove_operations"):
                    errors.append(f"{q}.{key}: unsupported in evolution 1.1")
                elif key in ("add_indexes", "contract_fields"):
                    if schema not in PHASED_SCHEMAS:
                        errors.append(f"{q}.{key}: needs schema {EVOLUTION_SCHEMA_12}")
                elif key == "rename_fields":
                    if schema not in RENAME_SCHEMAS:
                        errors.append(f"{q}.{key}: needs schema {EVOLUTION_SCHEMA_13} (renames as expand_contract, in transition)")
                elif key == "retire_fields":
                    if schema != EVOLUTION_SCHEMA_14:
                        errors.append(f"{q}.{key}: needs schema {EVOLUTION_SCHEMA_14} (the truth of a rename moves to the new column)")
                elif key not in ("name", "add_fields", "add_operations") and not str(key).startswith("x-"):
                    errors.append(f"{q}.{key}: unknown property")
            if "add_fields" in e and (not isinstance(e["add_fields"], list) or not e["add_fields"]):
                errors.append(f"{q}.add_fields: expected a non-empty list of fields")
            elif "add_fields" in e:
                for k, fld in enumerate(e["add_fields"]):
                    if isinstance(fld, dict) and "backfill" in fld:
                        if schema not in PHASED_SCHEMAS:
                            errors.append(f"{q}.add_fields[{k}].backfill: needs schema {EVOLUTION_SCHEMA_12}")
                        else:
                            errors.extend(validate_backfill(fld, f"{q}.add_fields[{k}]"))
            if "add_indexes" in e and schema in PHASED_SCHEMAS:
                e["add_indexes"], index_errors = validate_indexes(e["add_indexes"], f"{q}.add_indexes")
                errors.extend(index_errors)
            if "contract_fields" in e and schema in PHASED_SCHEMAS:
                e["contract_fields"], contract_errors = validate_contract_fields(e["contract_fields"], f"{q}.contract_fields")
                errors.extend(contract_errors)
            if "rename_fields" in e and schema in RENAME_SCHEMAS:
                e["rename_fields"], rename_errors = validate_rename_fields(e["rename_fields"], f"{q}.rename_fields")
                errors.extend(rename_errors)
            if "retire_fields" in e and schema == EVOLUTION_SCHEMA_14:
                e["retire_fields"], retire_errors = validate_contract_fields(e["retire_fields"], f"{q}.retire_fields")
                errors.extend(retire_errors)
            if "add_operations" in e:
                ops = e["add_operations"]
                if not isinstance(ops, list) or any(o not in OPERATIONS for o in ops):
                    errors.append(f"{q}.add_operations: expected a list of {', '.join(OPERATIONS)}")
            if not any(key in e for key in ("add_fields", "add_operations", "add_indexes", "contract_fields", "rename_fields", "retire_fields")):
                errors.append(f"{q}: nothing to add (add_fields, add_operations, add_indexes, contract_fields, rename_fields or retire_fields)")
        endpoints = m.get("add_endpoints", [])
        if not isinstance(endpoints, list):
            errors.append(f"{p}.add_endpoints: expected a list")
            endpoints = []
        for j, ep in enumerate(endpoints):
            q = f"{p}.add_endpoints[{j}]"
            if not isinstance(ep, dict):
                errors.append(f"{q}: expected an object")
                continue
            for key in ep:
                if key not in ("method", "path", "name", "summary", "response") and not str(key).startswith("x-"):
                    errors.append(f"{q}.{key}: unknown property")
            if ep.get("method") not in HTTP_METHODS:
                errors.append(f"{q}.method: expected one of {', '.join(HTTP_METHODS)}")
            if not isinstance(ep.get("path"), str) or (ep.get("path") != "" and not ep["path"].startswith("/")):
                errors.append(f"{q}.path: expected a path starting with '/' (or '' for the router root)")
            if not isinstance(ep.get("name"), str) or not IDENT_RE.match(ep.get("name", "")):
                errors.append(f"{q}.name: handler name must be a lowercase python identifier")
            if "response" not in ep:
                errors.append(f"{q}.response: required (a JSON value the handler returns)")
            else:
                try:
                    json.dumps(ep["response"])
                except (TypeError, ValueError):
                    errors.append(f"{q}.response: must be JSON-serialisable")
        fields = m.get("add_fields", [])
        if not isinstance(fields, list):
            errors.append(f"{p}.add_fields: expected a list")
            fields = []
        for j, f in enumerate(fields):
            q = f"{p}.add_fields[{j}]"
            if not isinstance(f, dict):
                errors.append(f"{q}: expected an object")
                continue
            for key in f:
                if key not in ("class", "name", "type", "required", "default") and not str(key).startswith("x-"):
                    errors.append(f"{q}.{key}: unknown property")
            if not isinstance(f.get("class"), str) or not re.match(r"^[A-Z][A-Za-z0-9]*$", f.get("class", "")):
                errors.append(f"{q}.class: expected a CapitalizedWord class name")
            if not isinstance(f.get("name"), str) or not IDENT_RE.match(f.get("name", "")):
                errors.append(f"{q}.name: field name must be a lowercase python identifier")
            if f.get("type") not in ("str", "int", "float", "bool"):
                errors.append(f"{q}.type: adopted classes accept str, int, float or bool fields")
            if f.get("required") is True and "default" not in f:
                errors.append(f"{q}: a required field on an existing class needs a default (existing callers do not send it)")
        if not deps and not entities and not endpoints and not fields:
            errors.append(f"{p}: nothing to change")
        out.append(ModuleChange(name=name, add_depends_on=list(deps), entities=list(entities), add_endpoints=list(endpoints), add_fields=list(fields)))
    return out


def validate_evolution(document: Any, source: Optional[str] = None) -> Tuple[Optional[EvolutionContract], List[str]]:
    errors: List[str] = []
    if not isinstance(document, dict):
        return None, ["top level: expected an object"]
    known = ("schema", "project", "add_modules", "modify_modules", "invariants", "files", "authorization")
    for key in document:
        if key in ("security", "persistence"):
            errors.append(
                f"evolution.{key}: not an evolution operation; authentication, the authorization policy, the tenant strategy and "
                "row-level security are design decisions: change them in a reviewed design contract with new tests (moving a "
                "project from application scoping to RLS, or back, is never done by an evolution)"
            )
        elif key not in known and not str(key).startswith("x-"):
            errors.append(f"evolution.{key}: unknown property (known: {', '.join(known)})")
    schema = document.get("schema")
    if schema not in EVOLUTION_SCHEMAS:
        errors.append(f"evolution.schema: expected one of {', '.join(repr(x) for x in EVOLUTION_SCHEMAS)}, got {schema!r}")
    if schema == "fastapi-architect/evolution/1.0" and "modify_modules" in document:
        errors.append("evolution.modify_modules: needs schema fastapi-architect/evolution/1.1")
    if "authorization" in document and schema not in PHASED_SCHEMAS:
        errors.append(f"evolution.authorization: needs schema {EVOLUTION_SCHEMA_12}")
    authorization, auth_errors = validate_authorization(document.get("authorization")) if schema in PHASED_SCHEMAS else ({}, [])
    errors.extend(auth_errors)
    if isinstance(document.get("authorization"), dict) and "destructive" in document["authorization"] and schema != EVOLUTION_SCHEMA_14:
        errors.append(f"evolution.authorization.destructive: needs schema {EVOLUTION_SCHEMA_14}")
    project = document.get("project")
    if not isinstance(project, dict):
        errors.append("evolution.project: required object with 'path'")
        project = {}
    for key in project:
        if key not in ("path", "package", "expected_spec_digest", "baseline_contract") and not str(key).startswith("x-"):
            errors.append(f"evolution.project.{key}: unknown property")
    path = project.get("path")
    if not isinstance(path, str) or not path.strip():
        errors.append("evolution.project.path: required non-empty string")
    package = project.get("package")
    if package is not None and (not isinstance(package, str) or not IDENT_RE.match(package)):
        errors.append(f"evolution.project.package: {package!r} must be a lowercase python identifier")
    expected = project.get("expected_spec_digest")
    if expected is not None and (not isinstance(expected, str) or not re.match(r"^[0-9a-f]{12,64}$", expected)):
        errors.append("evolution.project.expected_spec_digest: expected a hex digest (12 to 64 characters)")
    baseline = project.get("baseline_contract")
    if baseline is not None and (not isinstance(baseline, str) or not baseline.strip()):
        errors.append("evolution.project.baseline_contract: expected a path")
    modules = document.get("add_modules", [])
    if not isinstance(modules, list):
        errors.append("evolution.add_modules: expected a list of modules (same shape as spec.functional.modules)")
        modules = []
    for i, m in enumerate(modules):
        if not isinstance(m, dict):
            errors.append(f"evolution.add_modules[{i}]: expected an object")
        elif not isinstance(m.get("name"), str) or not IDENT_RE.match(m.get("name", "")):
            errors.append(f"evolution.add_modules[{i}].name: module name must be a lowercase python identifier")
    changes = _validate_module_changes(document.get("modify_modules"), errors, str(schema) if isinstance(schema, str) else None)
    if not modules and not changes and not any(e.startswith("evolution.modify_modules") or e.startswith("evolution.add_modules") for e in errors):
        errors.append("evolution: nothing to do (add_modules and modify_modules are both empty)")
    invariants = document.get("invariants", list(INVARIANTS))
    if not isinstance(invariants, list) or any(not isinstance(x, str) or x not in INVARIANTS for x in invariants):
        errors.append(f"evolution.invariants: expected a list of {', '.join(INVARIANTS)}")
        invariants = list(INVARIANTS)
    files = document.get("files", {})
    allow_modify: Optional[List[str]] = None
    preserve: List[str] = []
    if not isinstance(files, dict):
        errors.append("evolution.files: expected an object with allow_modify / preserve")
    else:
        for key in files:
            if key not in ("allow_modify", "preserve") and not str(key).startswith("x-"):
                errors.append(f"evolution.files.{key}: unknown property")
        for key in ("allow_modify", "preserve"):
            value = files.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(g, str) or not g.strip() for g in value):
                errors.append(f"evolution.files.{key}: expected a list of relative path globs")
                continue
            for g in value:
                if g.startswith(("/", "\\")) or ".." in g.split("/") or "\\" in g:
                    errors.append(f"evolution.files.{key}: {g!r} must stay inside the project (no absolute paths, no '..')")
            if key == "allow_modify":
                allow_modify = list(value)
            else:
                preserve = list(value)
    if errors:
        return None, sorted(set(errors))
    return EvolutionContract(schema=str(schema), project_path=str(path), package=package, expected_spec_digest=expected, baseline_contract=baseline, add_modules=modules, modify_modules=changes, invariants=list(invariants), allow_modify=allow_modify, preserve=preserve, source=source, document=json.loads(json.dumps(document)), authorization=authorization), []


def load_evolution(path: str) -> EvolutionContract:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise EvolutionError(path, [f"cannot read the contract: {exc}"])
    except ValueError as exc:
        raise EvolutionError(path, [f"the contract is not valid JSON: {exc}"])
    contract, errors = validate_evolution(document, path)
    if contract is None:
        raise EvolutionError(path, errors)
    return contract


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


@dataclass
class Precondition:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class EvolutionPlan:
    state: str
    project: str
    contract: EvolutionContract
    kind: str = "generated"  # generated | adopted
    package: Optional[str] = None
    style: Optional[str] = None
    baseline_spec_digest: Optional[str] = None
    evolved_spec_digest: Optional[str] = None
    added_modules: List[str] = field(default_factory=list)
    modified_modules: List[str] = field(default_factory=list)
    changes: List[Dict[str, Any]] = field(default_factory=list)  # classified changes
    preconditions: List[Precondition] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    create: List[str] = field(default_factory=list)
    modify: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    preserved_manual: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    public_contracts: Dict[str, Any] = field(default_factory=dict)
    migration: Dict[str, Any] = field(default_factory=dict)
    style_warning: Optional[str] = None
    risks: List[str] = field(default_factory=list)
    tests_required: List[str] = field(default_factory=list)
    diffs: Dict[str, str] = field(default_factory=dict)
    transforms: List[Dict[str, Any]] = field(default_factory=list)  # adopted: structural edit evidence
    phased_new: List[Dict[str, Any]] = field(default_factory=list)  # v2.1: phased changes this plan declares (expand)
    indexes_new: List[Dict[str, Any]] = field(default_factory=list)  # v2.1: concurrent indexes this plan creates
    contracting: List[Dict[str, Any]] = field(default_factory=list)  # v2.1: phased changes this plan contracts
    retiring: List[Dict[str, Any]] = field(default_factory=list)  # v2.2: renames whose truth this plan moves to the new column
    dropping: List[Dict[str, Any]] = field(default_factory=list)  # v2.2: retired renames whose old column this plan drops
    files: Dict[str, str] = field(default_factory=dict)
    expected: Dict[str, Optional[str]] = field(default_factory=dict)
    evolved_spec: Optional[Spec] = None
    discovery: Optional[ProjectModel] = None
    routes_before: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def operation_description(self) -> str:
        what = []
        if self.added_modules:
            what.append("add " + ", ".join(self.added_modules))
        if self.modified_modules:
            what.append("modify " + ", ".join(self.modified_modules))
        if self.contracting:
            what.append("contract " + ", ".join(f"{c['table']}.{c['column']}" for c in self.contracting))
        if self.retiring:
            what.append("retire " + ", ".join(f"{c['table']}.{c['from_column']}" for c in self.retiring))
        if self.dropping:
            what.append("drop " + ", ".join(f"{c['table']}.{c['from_column']}" for c in self.dropping))
        renames = [c for c in self.phased_new if c.get("kind") == "rename"]
        if renames:
            what.append("rename " + ", ".join(f"{c['table']}.{c['from_column']}->{c['column']}" for c in renames))
        return f"evolve ({self.kind}) {'; '.join(what)} [contract {self.contract.digest[:12]}]"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "project": self.project,
            "kind": self.kind,
            "contract": self.contract.to_dict(),
            "package": self.package,
            "style": self.style,
            "baseline_spec_digest": self.baseline_spec_digest,
            "evolved_spec_digest": self.evolved_spec_digest,
            "added_modules": list(self.added_modules),
            "modified_modules": list(self.modified_modules),
            "changes": list(self.changes),
            "preconditions": [p.to_dict() for p in self.preconditions],
            "blockers": list(self.blockers),
            "files": {"create": list(self.create), "modify": list(self.modify), "unchanged": len(self.unchanged), "kept": list(self.kept), "preserved_manual_changes": list(self.preserved_manual)},
            "conflicts": list(self.conflicts),
            "public_contracts": dict(self.public_contracts),
            "migration": dict(self.migration),
            "style_warning": self.style_warning,
            "risks": list(self.risks),
            "tests_required": list(self.tests_required),
            "diffs": dict(self.diffs),
            "transforms": list(self.transforms),
            "phased": {"new": list(self.phased_new), "indexes": list(self.indexes_new), "contracting": list(self.contracting), "retiring": list(self.retiring), "dropping": list(self.dropping)},
        }


def _glob_any(rel: str, globs: List[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in globs)


def _routes_of(spec: Spec, modules: List[str]) -> List[Dict[str, str]]:
    tenant = spec.security["multi_tenant"]["tenant_param"] if spec.multi_tenant else None
    out: List[Dict[str, str]] = []
    for m in spec.modules:
        if m.name not in modules:
            continue
        for e in m.entities:
            base = ("/api/v1/tenants/{" + tenant + "}" if tenant else "/api/v1") + f"/{e.plural}"
            ops = set(e.operations)
            if "create" in ops:
                out.append({"method": "POST", "path": base, "module": m.name})
            if "list" in ops:
                out.append({"method": "GET", "path": base, "module": m.name})
            if "get" in ops:
                out.append({"method": "GET", "path": f"{base}/{{{e.snake}_id}}", "module": m.name})
            if "update" in ops:
                out.append({"method": "PATCH", "path": f"{base}/{{{e.snake}_id}}", "module": m.name})
            if "delete" in ops:
                out.append({"method": "DELETE", "path": f"{base}/{{{e.snake}_id}}", "module": m.name})
    return out


def _resolve_project(contract: EvolutionContract) -> Path:
    return resolve_against(contract.project_path, contract.source)


def _module_files(layout: Layout, pkg: str, module: str) -> List[str]:
    out = [layout.path(c, module) for c in ("router", "schemas", "service", "repository", "models", "dependencies")] + [f"tests/test_{module}.py"]
    if not layout.by_layer:
        out += [f"src/{pkg}/modules/{module}/__init__.py", layout.path("ports", module), layout.path("adapters", module)]
    return out


def _apply_module_changes(baseline: Spec, doc: Dict[str, Any], changes: List[ModuleChange], pg: bool, plan: "EvolutionPlan", block, phased_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge modify_modules into the document; classify every change; block the unsafe ones. ``phased_ctx`` (v2.1)
    carries the manifest's phased changes and indexes and collects the ones this plan declares."""
    phased_ctx = phased_ctx if phased_ctx is not None else {"manifest_phased": [], "manifest_indexes": [], "head_before": None, "new": [], "indexes": [], "contracting": [], "retiring": [], "dropping": []}
    modules = {m.get("name"): m for m in doc.get("functional", {}).get("modules", []) if isinstance(m, dict)}
    existing_tables = {e.plural for m in baseline.modules for e in m.entities}
    for change in changes:
        target = modules.get(change.name)
        if target is None:
            block("module_exists", f"module {change.name!r} does not exist; use add_modules to create it")
            continue
        if change.add_endpoints or change.add_fields:
            block("change_supported_for_generated", f"module {change.name!r}: add_endpoints/add_fields with a class apply to adopted hand-written modules; generated modules change through entities[].add_fields / add_operations")
            continue
        for dep in change.add_depends_on:
            if dep not in modules:
                block("dependency_exists", f"module {change.name!r}: add_depends_on {dep!r} is not a module of the project")
            elif dep in target.get("depends_on", []):
                plan.changes.append({"kind": "add_depends_on", "target": f"{change.name} -> {dep}", "classification": COMPATIBLE, "detail": "already declared; no-op"})
            else:
                target.setdefault("depends_on", []).append(dep)
                plan.changes.append({"kind": "add_depends_on", "target": f"{change.name} -> {dep}", "classification": COMPATIBLE, "detail": "declared dependency added (cycles are rejected by the merged contract)"})
        entities = {e.get("name"): e for e in target.get("entities", []) if isinstance(e, dict)}
        for ech in change.entities:
            ent = entities.get(ech["name"])
            if ent is None:
                block("entity_exists", f"module {change.name!r}: entity {ech['name']!r} does not exist (adding entities to an existing module is not supported in 1.1)")
                continue
            existing_fields = {f.get("name") for f in ent.get("fields", []) if isinstance(f, dict)}
            table = next((e.plural for m in baseline.modules if m.name == change.name for e in m.entities if e.name == ech["name"]), None)
            for f in ech.get("add_fields", []):
                if not isinstance(f, dict) or not isinstance(f.get("name"), str):
                    block("field_valid", f"{change.name}.{ech['name']}: add_fields entries must be objects with a name")
                    continue
                label = f"{change.name}.{ech['name']}.{f['name']}"
                if f["name"] in existing_fields:
                    plan.changes.append({"kind": "add_field", "target": label, "classification": COMPATIBLE, "detail": "already present; no-op"})
                    continue
                backfill = f.get("backfill") if isinstance(f.get("backfill"), dict) else None
                if backfill is not None:
                    if not pg or table not in existing_tables:
                        block("backfill_needs_existing_postgresql_table", f"{label}: a backfill applies to an existing table of a PostgreSQL project; in memory or on a new table there is nothing to fill")
                        continue
                    strategy = str(backfill.get("strategy"))
                    params = backfill_params(backfill)
                    if strategy == "copy_column":
                        source = next((sf for sf in ent.get("fields", []) if isinstance(sf, dict) and sf.get("name") == params.get("source")), None)
                        if source is None:
                            block("backfill_source_exists", f"{label}: copy_column source {params.get('source')!r} is not a field of {ech['name']}")
                            continue
                        if source.get("type") != f.get("type"):
                            block("backfill_source_type", f"{label}: copy_column source {params.get('source')!r} has type {source.get('type')!r}, the new field {f.get('type')!r}")
                            continue
                    op_id = operation_id(table, f["name"], strategy, params, phased_ctx.get("head_before") or "")
                    if any(p.get("op_id") == op_id for p in phased_ctx["manifest_phased"]):
                        plan.changes.append({"kind": "add_field", "target": label, "classification": COMPATIBLE, "detail": f"phased change {op_id} already declared; no-op"})
                        continue
                    ent.setdefault("fields", []).append({k: v for k, v in f.items() if k != "backfill"})
                    phased_ctx["new"].append({"op_id": op_id, "module": change.name, "entity": ech["name"], "table": table, "column": f["name"], "field_type": f.get("type"), "required": bool(f.get("required", True)), "strategy": strategy, "params": params, "state_declared": "expand", "declared_on_head": phased_ctx.get("head_before"), "expand_revision": None, "index_revision": None, "contract_revision": None, "application_version": None})
                    plan.changes.append({"kind": "add_field", "target": label, "classification": COMPATIBLE, "detail": f"phased (operation {op_id}): added nullable without a server default (compatible with the running application); existing rows are filled by the generated backfill ({strategy}); NOT NULL only in a later, authorised contract phase"})
                    continue
                required = f.get("required", True)
                has_default = "default" in f and f.get("default") is not None
                if required and f.get("references") and pg and table in existing_tables:
                    plan.changes.append({"kind": "add_field", "target": label, "classification": POTENTIALLY_INCOMPATIBLE, "detail": "a NOT NULL foreign key cannot be backfilled; declare required: false"})
                    block("backfill_strategy", f"{label}: a NOT NULL reference cannot be added to an existing table")
                    continue
                if required and not has_default and pg and table in existing_tables:
                    plan.changes.append({"kind": "add_field", "target": label, "classification": POTENTIALLY_INCOMPATIBLE, "detail": "NOT NULL column on an existing table without a backfill strategy: declare `default` (becomes the server default) or `required: false`"})
                    block("backfill_strategy", f"{label}: NOT NULL column on an existing table needs a default or required: false")
                    continue
                if required and not has_default:
                    plan.changes.append({"kind": "add_field", "target": label, "classification": POTENTIALLY_INCOMPATIBLE, "detail": "required field without default: existing callers of the create endpoint would start failing with 422"})
                    block("compatible_request_contract", f"{label}: a new required field without default breaks existing callers; declare default or required: false")
                    continue
                ent.setdefault("fields", []).append(f)
                detail = "nullable column, optional in the request schema" if not required else "NOT NULL with a server default (backfill by default) and a default in the request schema"
                if f.get("references"):
                    detail += "; nullable foreign key to an existing table"
                plan.changes.append({"kind": "add_field", "target": label, "classification": COMPATIBLE, "detail": detail})
            tenant_column = baseline.security["multi_tenant"].get("tenant_column") if isinstance(baseline.security.get("multi_tenant"), dict) else None
            for item in ech.get("rename_fields", []) if isinstance(ech.get("rename_fields"), list) else []:
                source, target = str(item["from"]), str(item["to"])
                label = f"{change.name}.{ech['name']}.{source}->{target}"
                if not pg or table not in existing_tables:
                    block("rename_needs_existing_postgresql_table", f"{label}: a rename in transition applies to an existing table of a PostgreSQL project (in memory, or on a new table, declare the field under its final name)")
                    continue
                fields_now = [ff for ff in ent.get("fields", []) if isinstance(ff, dict)]
                if source in PROTECTED_COLUMNS or source == tenant_column or target == tenant_column or target in PROTECTED_COLUMNS:
                    block("rename_protected_column", f"{label}: {source if source in PROTECTED_COLUMNS or source == tenant_column else target!r} is a column the tool manages (identity, ownership, tenancy, timestamps); it is never renamed")
                    continue
                declared = next((p for p in phased_ctx["manifest_phased"] if p.get("kind") == KIND_RENAME and p.get("table") == table and p.get("from_column") == source and p.get("column") == target), None)
                if declared is not None:
                    plan.changes.append({"kind": "rename_field", "target": label, "classification": COMPATIBLE, "detail": f"rename {declared.get('op_id')} already declared ({declared.get('state_declared')}); no-op"})
                    continue
                in_transition = [p for p in phased_ctx["manifest_phased"] + phased_ctx["new"] if p.get("table") == table and p.get("state_declared") == "expand" and (p.get("column") in (source, target) or p.get("from_column") in (source, target))]
                if in_transition:
                    block("rename_column_in_transition", f"{label}: {source!r} or {target!r} already takes part in a phased change in transition ({', '.join(str(p.get('op_id')) for p in in_transition)}); finish it first")
                    continue
                old_doc = next((ff for ff in fields_now if ff.get("name") == source), None)
                if old_doc is None:
                    block("rename_source_exists", f"{label}: {source!r} is not a field of {ech['name']}")
                    continue
                if any(ff.get("name") == target for ff in fields_now):
                    block("rename_target_free", f"{label}: {target!r} already exists on {ech['name']}; a rename needs a free target name")
                    continue
                if old_doc.get("references"):
                    block("rename_protected_column", f"{label}: {source!r} is a foreign key; renaming a relation is not supported in this version")
                    continue
                indexed_by = [str(i.get("name")) for i in phased_ctx["manifest_indexes"] + phased_ctx["indexes"] if i.get("table") == table and source in (i.get("columns") or [])]
                if indexed_by:
                    block("rename_indexed_column_unsupported", f"{label}: {source!r} is a column of the concurrent index(es) {indexed_by}; an index on the old column would be dropped with it at the contract. Drop the index and declare it on the new name after the rename, or keep the name")
                    continue
                params = {"source": source}
                op_id = operation_id(table, target, MIRROR_STRATEGY, params, phased_ctx.get("head_before") or "")
                if any(p.get("op_id") == op_id for p in phased_ctx["manifest_phased"]):
                    plan.changes.append({"kind": "rename_field", "target": label, "classification": COMPATIBLE, "detail": f"rename {op_id} already declared; no-op"})
                    continue
                new_doc = dict(old_doc, name=target)
                ent["fields"] = [new_doc if ff.get("name") == source else ff for ff in fields_now]
                trigger, function = sync_names(table, op_id)
                phased_ctx["new"].append({"op_id": op_id, "kind": KIND_RENAME, "module": change.name, "entity": ech["name"], "table": table, "column": target, "from_column": source, "from_field": json.loads(json.dumps(old_doc)), "field_type": old_doc.get("type"), "required": bool(old_doc.get("required", True)), "strategy": MIRROR_STRATEGY, "params": params, "state_declared": "expand", "declared_on_head": phased_ctx.get("head_before"), "expand_revision": None, "index_revision": None, "contract_revision": None, "application_version": None, "sync": {"mechanism": "trigger", "trigger": trigger, "function": function}, "source_of_truth": "from_column", "read_policy": "from_column", "write_policy": "both", "transition": "A (old name only) and B (both names) coexist on the expanded schema; retire and contract are not available in this version"})
                plan.changes.append({"kind": "rename_field", "target": label, "classification": COMPATIBLE, "detail": f"rename in transition (operation {op_id}): {target!r} is added nullable next to {source!r}; a sync trigger keeps both equal for every write of either application version; the backfill reconciles the rows written before it; the old column stays the source of truth and nothing is dropped in this version"})
            names_now = {ff.get("name") for ff in ent.get("fields", []) if isinstance(ff, dict)}
            for spec_index in ech.get("add_indexes", []) if isinstance(ech.get("add_indexes"), list) else []:
                columns = list(spec_index.get("columns", []))
                label = f"{change.name}.{ech['name']}:index({','.join(columns)})"
                if not pg or table not in existing_tables:
                    block("index_needs_existing_postgresql_table", f"{label}: concurrent indexes apply to existing tables of PostgreSQL projects (a new table gets its indexes in the revision that creates it)")
                    continue
                missing = [c for c in columns if c not in names_now]
                if missing:
                    block("index_columns_exist", f"{label}: unknown column(s) {missing}")
                    continue
                name = index_name(table, columns)
                if any(i.get("name") == name for i in phased_ctx["manifest_indexes"] + phased_ctx["indexes"]):
                    plan.changes.append({"kind": "add_index", "target": label, "classification": COMPATIBLE, "detail": f"index {name} already declared; no-op"})
                    continue
                phased_ctx["indexes"].append({"name": name, "table": table, "columns": columns, "unique": bool(spec_index.get("unique", False))})
                plan.changes.append({"kind": "add_index", "target": label, "classification": COMPATIBLE, "detail": f"CREATE INDEX CONCURRENTLY {name} in its own non-transactional revision: writes continue; an invalid leftover is dropped and rebuilt, another definition under the same name is refused"})
            for item in ech.get("contract_fields", []) if isinstance(ech.get("contract_fields"), list) else []:
                label = f"{change.name}.{ech['name']}.{item['name']}:contract"
                pending = next((p for p in phased_ctx["manifest_phased"] if p.get("op_id") == item["operation"]), None)
                if pending is None or pending.get("table") != table or pending.get("column") != item["name"]:
                    block("contract_operation_known", f"{label}: operation {item['operation']} is not a phased change of {table}.{item['name']} recorded in the manifest")
                    continue
                if pending.get("kind") == KIND_RENAME:
                    if pending.get("state_declared") == "expand":
                        block("contract_rename_retire_first", f"{label}: operation {item['operation']} is a rename in transition; declare it in retire_fields first (the truth moves to the new column, nothing is dropped), deploy that generation, verify, and only then contract")
                    elif pending.get("state_declared") != PHASE_RETIRE:
                        block("contract_operation_pending", f"{label}: operation {item['operation']} is already declared {pending.get('state_declared')!r}")
                    elif any(d.get("op_id") == pending.get("op_id") for d in phased_ctx["dropping"]):
                        continue
                    else:
                        phased_ctx["dropping"].append(dict(pending))
                        plan.changes.append({"kind": "drop_column", "target": label, "classification": DESTRUCTIVE, "detail": f"destructive contract of operation {item['operation']}: DROP COLUMN {table}.{pending.get('from_column')} RESTRICT with its sync trigger and function, in one transaction, after sixteen named preconditions; the downgrade recreates the column from the new column's current values (offline); B is incompatible from here on"})
                    continue
                if pending.get("state_declared") != "expand":
                    block("contract_operation_pending", f"{label}: operation {item['operation']} is already declared {pending.get('state_declared')!r}")
                    continue
                field_doc = next((ff for ff in ent.get("fields", []) if isinstance(ff, dict) and ff.get("name") == item["name"]), None)
                if field_doc is None or field_doc.get("required", True) is not True:
                    block("contract_field_required", f"{label}: the field must be declared required (its desired state) to become NOT NULL")
                    continue
                phased_ctx["contracting"].append(dict(pending))
                plan.changes.append({"kind": "contract_not_null", "target": label, "classification": POTENTIALLY_INCOMPATIBLE, "detail": f"NOT NULL transition of operation {item['operation']}: needs the backfill complete, zero pending rows, the application version that writes the column, fresh evidence and explicit authorization; online (CHECK NOT VALID, VALIDATE, SET NOT NULL, DROP CONSTRAINT); reversible (DROP NOT NULL)"})
            for item in ech.get("retire_fields", []) if isinstance(ech.get("retire_fields"), list) else []:
                label = f"{change.name}.{ech['name']}.{item['name']}:retire"
                pending = next((p for p in phased_ctx["manifest_phased"] if p.get("op_id") == item["operation"]), None)
                if pending is None or pending.get("table") != table or pending.get("column") != item["name"]:
                    block("retire_operation_known", f"{label}: operation {item['operation']} is not a phased change of {table}.{item['name']} recorded in the manifest")
                    continue
                if pending.get("kind") != KIND_RENAME:
                    block("retire_rename_only", f"{label}: operation {item['operation']} is not a rename; an added column has no name to retire (contract_fields makes it NOT NULL)")
                    continue
                if pending.get("state_declared") != "expand":
                    block("retire_operation_pending", f"{label}: operation {item['operation']} is already declared {pending.get('state_declared')!r}")
                    continue
                if any(r.get("op_id") == pending.get("op_id") for r in phased_ctx["retiring"]):
                    continue
                phased_ctx["retiring"].append(dict(pending))
                plan.changes.append({"kind": "retire_rename", "target": label, "classification": POTENTIALLY_INCOMPATIBLE, "detail": f"retire of operation {item['operation']}: the code drops the former name {pending.get('from_column')!r} (requests and responses carry {item['name']!r} only), the retire revision moves the truth to {item['name']} (NOT NULL and default carried over, function comment stamped), the old column stays and the trigger keeps it equal for the transitional version; needs fresh evidence (backfill complete, columns equal, trigger enabled) and authorization.contract.declared_by"})
            current_ops = ent.get("operations")
            current_set = set(current_ops) if isinstance(current_ops, list) else set(OPERATIONS)
            for op in ech.get("add_operations", []):
                label = f"{change.name}.{ech['name']}:{op}"
                if op in current_set:
                    plan.changes.append({"kind": "add_operation", "target": label, "classification": COMPATIBLE, "detail": "already present; no-op"})
                    continue
                current_set.add(op)
                plan.changes.append({"kind": "add_operation", "target": label, "classification": COMPATIBLE, "detail": "new use case, route and tests; existing routes unchanged"})
            if isinstance(current_ops, list) or current_set != set(OPERATIONS):
                ent["operations"] = sorted(current_set, key=OPERATIONS.index)
    return doc


def plan_evolution(contract: EvolutionContract, *, baseline_contract: Optional[str] = None, tool_version: str = TOOL_VERSION, authorize_contract: bool = False, evidence_path: Optional[str] = None, confirm_irreversible: Optional[str] = None) -> EvolutionPlan:
    """Compute the plan without writing anything."""
    raw = _resolve_project(contract)
    plan = EvolutionPlan(state=STATE_PLANNED, project=str(raw), contract=contract)
    pre = plan.preconditions

    def block(name: str, detail: str) -> None:
        pre.append(Precondition(name, False, detail))
        plan.blockers.append(f"{name}: {detail}")

    if raw.is_symlink():
        block("project_is_not_a_symlink", f"{raw} is a symlink")
        plan.state = STATE_BLOCKED
        return plan
    if not raw.is_dir():
        block("project_exists", f"{raw} is not a directory")
        plan.state = STATE_BLOCKED
        return plan
    project = raw.resolve()
    plan.project = str(project)
    pending = pending_operations(project)
    if pending:
        block("no_pending_operation", "an interrupted operation exists (" + ", ".join(f"{op.op_id} {op.state}" for op in pending) + "); run `evolve_fastapi.py recover` before planning")
        plan.state = STATE_BLOCKED
        return plan
    unreadable = unreadable_operations(project)
    if unreadable:
        block("no_unreadable_operation", "an operation directory has no readable journal (" + ", ".join(f"{u['op_id']}: {u['reason']}" for u in unreadable) + "); inspect .fastapi-architect/ops before planning: the staged files and backups are kept, nothing is guessed")
        plan.state = STATE_BLOCKED
        return plan
    model = discover(str(project))
    plan.discovery = model
    plan.routes_before = list(model.routes)
    pre.append(Precondition("project_is_not_a_symlink", True, str(project)))
    if model.manifest is None and model.adoption is not None:
        plan.kind = "adopted"
        return _plan_adopted(plan, model, block)
    if model.manifest is None:
        block("generation_manifest_present", "no .fastapi-architect/generation.json and no adoption baseline: the project was neither generated by this tool nor adopted (`evolve_fastapi.py propose` / `adopt`)")
        plan.state = STATE_BLOCKED
        return plan
    pre.append(Precondition("generation_manifest_present", True, model.manifest.get("schema", "?")))
    baseline: Optional[Spec] = model.baseline_spec
    baseline_path = baseline_contract or contract.baseline_contract
    if baseline_path:
        candidate = Path(baseline_path)
        if not candidate.is_absolute() and contract.source:
            candidate = Path(contract.source).resolve().parent / candidate
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
            spec, errors = validate_spec(doc, str(candidate))
        except (OSError, ValueError) as exc:
            spec, errors = None, [str(exc)]
        if spec is None:
            block("baseline_contract_valid", f"{candidate}: " + "; ".join(errors[:3]))
        elif spec.digest != (model.manifest.get("spec") or {}).get("digest"):
            block("baseline_contract_matches_manifest", f"{candidate} has digest {spec.digest[:12]}, the manifest records {(model.manifest.get('spec') or {}).get('digest', '?')[:12]}: stale or wrong baseline")
        else:
            baseline = spec
            pre.append(Precondition("baseline_contract_matches_manifest", True, spec.digest[:12]))
    if baseline is None and not plan.blockers:
        block("baseline_contract_available", "the manifest has no spec_document (generated before 1.1) and no baseline_contract was given")
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    assert baseline is not None
    plan.baseline_spec_digest = baseline.digest
    plan.package = baseline.package
    plan.style = model.manifest.get("style")
    if contract.expected_spec_digest and not baseline.digest.startswith(contract.expected_spec_digest):
        block("expected_spec_digest", f"the contract expects baseline {contract.expected_spec_digest[:12]}, the project is at {baseline.digest[:12]}: stale baseline")
    else:
        pre.append(Precondition("expected_spec_digest", True, "not declared" if not contract.expected_spec_digest else baseline.digest[:12]))
    if contract.package and contract.package != baseline.package:
        block("package_matches", f"the contract targets package {contract.package!r}, the project is {baseline.package!r}")
    if model.package != baseline.package:
        block("package_present", f"package {baseline.package!r} recorded in the manifest was not found on disk (found {model.package!r})")
    expected_layout = "layered" if plan.style == "layered" else "modular"
    if model.layout != expected_layout:
        block("layout_matches_manifest", f"the manifest says style {plan.style!r} but the layout on disk is {model.layout!r}")
    else:
        pre.append(Precondition("layout_matches_manifest", True, f"{plan.style} / {model.layout}"))
    missing = sorted(p for p, s in model.ownership.items() if s == "missing_managed")
    if missing:
        block("managed_files_present", "managed files are missing: " + ", ".join(missing[:10]))

    existing = [m.name for m in baseline.modules]
    baseline_modules = {m.get("name"): m for m in baseline.document.get("functional", {}).get("modules", []) if isinstance(m, dict)}
    already = [m for m in contract.add_modules if m.get("name") in baseline_modules]
    for m in already:
        if json.dumps(baseline_modules[m["name"]], sort_keys=True) != json.dumps(m, sort_keys=True):
            block("module_is_new", f"module {m.get('name')!r} already exists with a different definition; describe the change with modify_modules")
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    new_modules = [m for m in contract.add_modules if m.get("name") not in baseline_modules]
    offset = len(existing)
    pg = baseline.persistence["kind"] == "postgresql"
    evolved_doc = json.loads(json.dumps(baseline.document))
    evolved_doc.setdefault("functional", {}).setdefault("modules", [])
    evolved_doc["functional"]["modules"] = list(evolved_doc["functional"]["modules"]) + list(new_modules)  # new modules first: changes may depend on them
    manifest_chain = [m for m in (model.manifest.get("migrations") or []) if isinstance(m, dict)]
    head_before = str(manifest_chain[-1].get("revision")) if manifest_chain else None
    phased_ctx: Dict[str, Any] = {"manifest_phased": [dict(x) for x in (model.manifest.get("phased_changes") or []) if isinstance(x, dict)], "manifest_indexes": [dict(x) for x in (model.manifest.get("concurrent_indexes") or []) if isinstance(x, dict)], "head_before": head_before, "new": [], "indexes": [], "contracting": [], "retiring": [], "dropping": []}
    evolved_doc = _apply_module_changes(baseline, evolved_doc, contract.modify_modules, pg, plan, block, phased_ctx)
    plan.phased_new, plan.indexes_new, plan.contracting = phased_ctx["new"], phased_ctx["indexes"], phased_ctx["contracting"]
    plan.retiring, plan.dropping = phased_ctx["retiring"], phased_ctx["dropping"]
    if plan.dropping and "no_destructive_schema_change" in contract.invariants:
        block("no_destructive_schema_change", "the contract declares no_destructive_schema_change and asks to drop " + ", ".join(f"{d['table']}.{d['from_column']}" for d in plan.dropping) + "; a destructive contract must leave that invariant out, on purpose")
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    if plan.contracting or plan.retiring or plan.dropping:
        auth = contract.authorization.get("contract") or {}
        destructive_auth = contract.authorization.get("destructive") or {}
        evidence_file = evidence_path or auth.get("evidence")
        evidence, evidence_errors = (None, ["no evidence path given (authorization.contract.evidence or --evidence)"])
        if evidence_file:
            evidence, evidence_errors = load_evidence(str(resolve_against(str(evidence_file), contract.source)))
        if evidence is None:
            block("contract_evidence_valid", "; ".join(evidence_errors))
        max_age = float(auth.get("max_evidence_age_hours") or 24.0)
        for change in plan.contracting:
            for name, ok, detail in contract_preconditions(change, evidence, head_before=head_before, authorized=authorize_contract, declared_by=auth.get("declared_by"), max_age_hours=max_age, indexes=phased_ctx["manifest_indexes"]):
                pre.append(Precondition(name, ok, detail))
                if not ok:
                    block(name, f"{change['table']}.{change['column']}: {detail}")
        for change in plan.retiring:
            for name, ok, detail in retire_preconditions(change, evidence, head_before=head_before, declared_by=auth.get("declared_by"), max_age_hours=max_age, indexes=phased_ctx["manifest_indexes"]):
                pre.append(Precondition(name, ok, detail))
                if not ok:
                    block(name, f"{change['table']}.{change['from_column']} -> {change['column']}: {detail}")
        if plan.dropping:
            holder = inspect_lock(project)
            alembic_now = model.alembic or {}
            heads_on_disk = len(alembic_now.get("heads") or []) if alembic_now.get("chain_ok") else 0
            drift_clean = not any(state == "managed_modified" for state in model.ownership.values())
            phrase = confirmation_phrase(plan.dropping)
            for change in plan.dropping:
                for name, ok, detail in drop_preconditions(change, evidence, head_before=head_before, authorized=authorize_contract, declared_by=auth.get("declared_by"), max_age_hours=max_age, destructive=destructive_auth, confirm_phrase=confirm_irreversible, expected_phrase=phrase, package=baseline.package, lock_holder=holder if holder and holder.get("status") == "held" else None, drift_clean=drift_clean, heads_on_disk=heads_on_disk):
                    pre.append(Precondition(name, ok, detail))
                    if not ok:
                        block(name, f"{change['table']}.{change['from_column']}: {detail}")
        if plan.blockers:
            plan.state = STATE_BLOCKED
            return plan
    if canonical_digest(evolved_doc) == baseline.digest and not (plan.indexes_new or plan.contracting or plan.retiring or plan.dropping):
        plan.state = STATE_NOOP
        plan.evolved_spec_digest = baseline.digest
        pre.append(Precondition("already_applied", True, "the contract adds nothing the project does not already have; nothing to do"))
        plan.public_contracts = {"routes_added": [], "routes_removed": [], "routes_changed": [], "declared_contracts_changed": False}
        plan.migration = {"required": False, "kind": "none (already applied)"}
        return plan
    evolved, errors = validate_spec(evolved_doc, "evolved contract")
    if errors:
        mapped = []
        for e in errors:
            match = re.match(r"spec\.functional\.modules\[(\d+)\](.*)", e)
            if match and int(match.group(1)) >= offset:
                mapped.append(f"evolution.add_modules[{int(match.group(1)) - offset}]{match.group(2)}")
            elif match:
                mapped.append(f"evolution.modify_modules ({existing[int(match.group(1))]}){match.group(2)}")
            else:
                mapped.append(e)
        block("merged_contract_valid", "; ".join(mapped))
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    assert evolved is not None
    plural_owner: Dict[str, str] = {}
    for m in evolved.modules:
        for e in m.entities:
            if e.plural in plural_owner and plural_owner[e.plural] != m.name:
                block("routes_are_unique", f"entity {e.name!r} of module {m.name!r} would mount /{e.plural}, already used by module {plural_owner[e.plural]!r}")
            plural_owner.setdefault(e.plural, m.name)
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan

    rec = recommend(evolved)
    if rec.style != plan.style:
        plan.style_warning = f"the advisor would now recommend '{rec.style}' for {len(evolved.modules)} modules; the project keeps its '{plan.style}' layout (migrating the layout is a separate, explicit operation)"
        evolved_doc.setdefault("architecture", {})["style_preference"] = plan.style
        evolved, errors = validate_spec(evolved_doc, "evolved contract")
        assert evolved is not None and not errors, errors
        rec = recommend(evolved)
    plan.evolved_spec = evolved
    plan.evolved_spec_digest = evolved.digest
    plan.added_modules = [m.get("name") for m in new_modules]
    plan.modified_modules = [c.name for c in contract.modify_modules]

    lineage: Optional[Lineage] = None
    if pg:
        chain = model.manifest.get("migrations") or []
        alembic = model.alembic or {}
        if not chain:
            block("migration_lineage_known", "the manifest does not record the migration chain (generated before 1.1); the tables covered by the existing revisions cannot be known")
        elif not alembic or not alembic.get("chain_ok"):
            block("alembic_chain_is_linear", f"heads on disk: {alembic.get('heads')}, unknown parents: {alembic.get('unknown_parents')}" if alembic else "no alembic.ini found")
        elif alembic.get("heads") != [chain[-1]["revision"]]:
            block("alembic_head_matches_manifest", f"head on disk {alembic.get('heads')} differs from the manifest's {chain[-1]['revision']}")
        elif alembic.get("historical_modified"):
            block("historical_migrations_unchanged", "modified since generation: " + ", ".join(alembic["historical_modified"]))
        else:
            tables = sorted({t for entry in chain for t in entry.get("tables", [])})
            lineage = Lineage(head=chain[-1]["revision"], files=[entry["file"] for entry in chain], tables=tables, revisions=list(chain), baseline=baseline)
            pre.append(Precondition("alembic_chain_is_linear", True, f"head {lineage.head}, {len(chain)} revision(s)"))
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan

    baseline_rec = recommend(baseline)
    baseline_phases = PhaseState(pending=[x for x in phased_ctx["manifest_phased"] if x.get("state_declared") == "expand"], all_phased=list(phased_ctx["manifest_phased"]), all_indexes=list(phased_ctx["manifest_indexes"]), head=head_before)
    old_files = build(baseline, baseline_rec, phases=baseline_phases).files
    phases: Optional[PhaseState] = None
    expand_rev = index_rev = contract_rev = retire_rev = drop_rev = None
    if pg and lineage is not None:
        if (plan.phased_new or plan.indexes_new or plan.contracting or plan.retiring or plan.dropping) and not evolved.persistence["kind"] == "postgresql":
            block("phased_needs_postgresql", "phased changes need PostgreSQL persistence")
        head = lineage.head
        tables_now = set(lineage.tables)
        if any(e.plural not in tables_now for m in evolved.modules for e in m.entities) or added_columns(evolved, lineage):
            expand_rev = delta_revision(lineage, evolved)
            head = expand_rev
        if plan.indexes_new:
            index_rev = index_revision(head, plan.indexes_new)
            head = index_rev
        if plan.retiring:
            retire_rev = retire_revision(head, [str(c["op_id"]) for c in plan.retiring])
            head = retire_rev
        if plan.contracting:
            contract_rev = contract_revision(head, [str(c["op_id"]) for c in plan.contracting])
            head = contract_rev
        if plan.dropping:
            drop_rev = drop_revision(head, [str(c["op_id"]) for c in plan.dropping])
            head = drop_rev
        for op in plan.phased_new:
            op.update(expand_revision=expand_rev, index_revision=index_rev, application_version=evolved.digest)
        contracting_ids = {str(c["op_id"]) for c in plan.contracting}
        retiring_ids = {str(c["op_id"]) for c in plan.retiring}
        dropping_ids = {str(c["op_id"]) for c in plan.dropping}
        destructive_auth = contract.authorization.get("destructive") or {}
        all_phased: List[Dict[str, Any]] = []
        for x in phased_ctx["manifest_phased"]:
            entry = dict(x)
            if entry.get("op_id") in contracting_ids:
                entry.update(state_declared="contract", contract_revision=contract_rev)
            elif entry.get("op_id") in retiring_ids:
                entry.update(state_declared=PHASE_RETIRE, retire_revision=retire_rev, application_version=evolved.digest, source_of_truth="to_column", read_policy="to_column", write_policy="to_column", transition="retired: the code knows the new name only; the trigger keeps the old column equal for the transitional version until the contract")
            elif entry.get("op_id") in dropping_ids:
                _trigger, _function = sync_names(str(entry["table"]), str(entry["op_id"]))
                entry.update(state_declared="contract", contract_revision=drop_rev, dropped={"column": entry.get("from_column"), "trigger": _trigger, "function": _function}, destructive={"approved_by": destructive_auth.get("approved_by"), "declared_by": (contract.authorization.get("contract") or {}).get("declared_by"), "confirmation": confirmation_phrase(plan.dropping), "evidence_generated_at": (evidence.get("generated_at") if isinstance(evidence, dict) else None)})
            all_phased.append(entry)
        all_phased.extend(plan.phased_new)
        tag = application_tag(evolved, all_phased)
        for entry in all_phased:
            if entry.get("kind") == KIND_RENAME and entry.get("state_declared") == PHASE_RETIRE:
                tags = [str(t) for t in (entry.get("compatible_tags") or [])]
                entry["compatible_tags"] = tags + ([tag] if tag not in tags else [])  # generations that do not map the old column
        plan.contracting = [x for x in all_phased if x.get("op_id") in contracting_ids]
        plan.retiring = [x for x in all_phased if x.get("op_id") in retiring_ids]
        plan.dropping = [x for x in all_phased if x.get("op_id") in dropping_ids]
        all_indexes = list(phased_ctx["manifest_indexes"]) + [dict(i, revision=index_rev) for i in plan.indexes_new]
        drop_authorization = {"approved_by": destructive_auth.get("approved_by"), "phrase": confirmation_phrase(plan.dropping), "reviewed_functions": list(destructive_auth.get("reviewed_functions") or [])} if plan.dropping else {}
        phases = PhaseState(pending=[x for x in all_phased if x.get("state_declared") == "expand"], all_phased=all_phased, all_indexes=all_indexes, new_phased=list(plan.phased_new), new_indexes=list(plan.indexes_new), contracting=list(plan.contracting), retiring=list(plan.retiring), dropping=list(plan.dropping), drop_authorization=drop_authorization, head=head_before)
    elif plan.phased_new or plan.indexes_new or plan.contracting or plan.retiring or plan.dropping:
        block("phased_needs_migration_lineage", "phased changes, concurrent indexes and contract phases need a PostgreSQL project with a known migration chain")
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    new_bp = build(evolved, rec, lineage, phases)
    new_files = dict(new_bp.files)
    history = list(model.manifest.get("history") or [{"operation": "generate", "spec_digest": baseline.digest, "modules": existing}])
    history.append({"operation": "evolve", "contract_digest": contract.digest, "previous_spec_digest": baseline.digest, "spec_digest": evolved.digest, "added_modules": plan.added_modules, "modified_modules": plan.modified_modules, "phased": {"expanded": [x["op_id"] for x in plan.phased_new if x.get("kind") != KIND_RENAME], "renamed": [x["op_id"] for x in plan.phased_new if x.get("kind") == KIND_RENAME], "indexes": [x["name"] for x in plan.indexes_new], "contracted": [x["op_id"] for x in plan.contracting], "retired": [x["op_id"] for x in plan.retiring], "dropped": [x["op_id"] for x in plan.dropping]}})
    manifest = build_manifest(evolved, new_bp, tool_version, history=history)
    if lineage:
        for rel in lineage.files:
            manifest["files"][rel] = model.manifest["files"].get(rel) or model.digests.get(rel)
    new_files[MANIFEST] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    old_files[MANIFEST] = json.dumps(model.manifest, indent=2, sort_keys=True) + "\n"
    layout = Layout(evolved, rec)
    allow = contract.allow_modify if contract.allow_modify is not None else [p.format(pkg=baseline.package) for p in DEFAULT_INTEGRATION_POINTS]
    allow.append(MANIFEST)
    for name in plan.modified_modules:
        allow.extend(_module_files(layout, baseline.package, name))
    manifest_path = project / MANIFEST
    for rel in sorted(new_files):
        content = new_files[rel]
        on_disk = model.digests.get(rel)
        status = model.ownership_of(rel)
        try:
            safe_join(project, rel)
        except GenerationError as exc:
            plan.conflicts.append({"path": rel, "kind": "unsafe_path", "detail": str(exc)})
            continue
        if rel == MANIFEST:
            plan.modify.append(rel)
            plan.files[rel] = content
            plan.expected[rel] = sha256_bytes(manifest_path.read_bytes())
            continue
        if rel not in old_files:
            if on_disk is not None:
                plan.conflicts.append({"path": rel, "kind": "file_in_the_way", "detail": f"a {status} file already exists where a new file would be created"})
            else:
                plan.create.append(rel)
                plan.files[rel] = content
                plan.expected[rel] = None
            continue
        if content == old_files[rel]:
            if status == "managed_modified":
                plan.preserved_manual.append(rel)
            plan.unchanged.append(rel)
            continue
        if _glob_any(rel, contract.preserve):
            plan.conflicts.append({"path": rel, "kind": "preserved_file_would_change", "detail": "listed under files.preserve but the evolution needs to regenerate it"})
            continue
        if not _glob_any(rel, allow):
            plan.conflicts.append({"path": rel, "kind": "not_allowed_to_modify", "detail": "the evolution needs to regenerate it but it is not an allowed integration point (files.allow_modify)"})
            continue
        if status == "managed_modified":
            plan.conflicts.append({"path": rel, "kind": "manual_change_on_integration_point", "detail": "generated file edited by hand; regenerating it would destroy the manual change (merge it into the contract or restore the file first)"})
            continue
        if status in ("unmanaged", "unknown"):
            plan.conflicts.append({"path": rel, "kind": "unmanaged_file_in_the_way", "detail": "the file is not listed in the manifest; it is not known to be safe to regenerate"})
            continue
        if on_disk is None:
            plan.conflicts.append({"path": rel, "kind": "missing_managed_file", "detail": "listed in the manifest but missing on disk"})
            continue
        plan.modify.append(rel)
        plan.files[rel] = content
        plan.expected[rel] = on_disk
        old_text = (project / rel).read_text(encoding="utf-8", errors="replace")
        plan.diffs[rel] = "\n".join(difflib.unified_diff(old_text.splitlines(), content.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=2))[:20000]
    if lineage:
        plan.kept = list(lineage.files)
    for rel, status in sorted(model.ownership.items()):
        if status == "managed_modified" and rel not in plan.preserved_manual and rel not in plan.modify and rel not in [c["path"] for c in plan.conflicts]:
            plan.preserved_manual.append(rel)
    for c in plan.conflicts:
        plan.blockers.append(f"{c['kind']}: {c['path']}")

    before = _routes_of(baseline, existing)
    after = _routes_of(evolved, [m.name for m in evolved.modules])
    added = [r for r in after if r not in before]
    removed = [r for r in before if r not in after]
    plan.public_contracts = {"routes_added": added, "routes_removed": removed, "routes_changed": [], "declared_contracts_changed": new_files.get(".audit-fastapi.json") != old_files.get(".audit-fastapi.json")}
    if removed and "existing_routes_unchanged" in contract.invariants:
        block("existing_routes_unchanged", f"{len(removed)} existing route(s) would disappear")
    if pg and lineage:
        delta = [m for m in new_bp.migrations if m["revision"] not in {r["revision"] for r in lineage.revisions}]
        columns = added_columns(evolved, lineage)
        plan.migration = {"required": bool(delta), "head_before": lineage.head, "new_revision": delta[0]["revision"] if delta else None, "file": delta[0]["file"] if delta else None, "tables": delta[0]["tables"] if delta else [], "columns": [f"{t}.{f.name}" + (" (NOT NULL, server default)" if f.required else " (nullable)") for t, f, _r in columns], "kind": "additive (create_table, add_column nullable or with server default, foreign keys, indexes); no drop, no alter, no data migration" if delta else "none", "historical_files_kept": list(lineage.files)}
        plan.migration.update({
            "head_after": delta[-1]["revision"] if delta else lineage.head,
            "revisions": [{"revision": x["revision"], "file": x["file"], "phase": x.get("phase", "additive"), "transactional": x.get("transactional", True), "reversible": x.get("reversible", True)} for x in delta],
            "phases": phase_descriptions(evolved, plan.phased_new, plan.indexes_new, plan.contracting, lineage.head, expand_rev, index_rev, contract_rev, retiring=plan.retiring, retire_rev=retire_rev, dropping=plan.dropping, drop_rev=drop_rev),
            "backfills": [{"operation": op["op_id"], "table": op["table"], "column": op["column"], "strategy": op["strategy"], "params": op["params"], "application_version": op["application_version"], "run": f"python -m {evolved.package}.ops.backfill run {op['op_id']}", "verify": f"python -m {evolved.package}.ops.backfill verify --application-version {op['application_version']} --output evidence.json", "state": "declared: expand; applied and verified states are read from the database, never from the repository"} for op in plan.phased_new],
            "renames": [{"operation": op["op_id"], "table": op["table"], "from": op["from_column"], "to": op["column"], "strategy": "expand_contract", "sync": op["sync"], "source_of_truth": "the old column until the rename is retired", "read_policy": "the transitional application reads the old column and exposes both names (old one deprecated)", "write_policy": "the transitional application writes both; the old application writes the old one; the sync trigger mirrors either write, the old column winning when one statement sets both", "coexistence": "A (old name only) and B (both names) may run together; C (new name only) needs the retire phase, not available in this version", "next": "retire (remove the old name from the code) and contract (drop the old column): later increments, each an authorised evolution", "verify": f"python -m {evolved.package}.ops.backfill verify: mismatch_rows must be 0 and the sync trigger enabled"} for op in plan.phased_new if op.get("kind") == KIND_RENAME],
            "retires": [{"operation": op["op_id"], "table": op["table"], "from": op["from_column"], "to": op["column"], "revision": retire_rev, "reversible": True, "source_of_truth": "the new column from the retire revision on", "read_policy": "C reads the new column; B keeps reading the old one, kept equal by the trigger", "write_policy": "C writes the new column and the trigger fills the old one; B writes both; a statement setting both names to different values is refused", "coexistence": "B and C on the retire schema; A is outside this phase's contract and must be gone before the destructive contract", "compatible_tags": list(op.get("compatible_tags") or [])} for op in plan.retiring],
            "contract": [{"operation": op["op_id"], "table": op["table"], "column": op["column"], "revision": contract_rev, "reversible": True} for op in plan.contracting] + [{"operation": op["op_id"], "table": op["table"], "column": op["from_column"], "revision": drop_rev, "destructive": True, "reversible": "conditional", "drops": dict(op.get("dropped") or {}), "confirmation": confirmation_phrase(plan.dropping), "approved_by": (op.get("destructive") or {}).get("approved_by")} for op in plan.dropping],
        })
        if plan.phased_new or plan.indexes_new or plan.contracting or plan.retiring or plan.dropping:
            plan.migration["kind"] = "phased: " + ", ".join(p["phase"] for p in plan.migration["phases"]) + " (expand adds nullable columns; the backfill runs out of band; the contract phase is a separate, authorised evolution)"
    else:
        plan.migration = {"required": False, "kind": "not applicable (in-memory persistence)" if not pg else "unknown"}
    if plan.style_warning:
        plan.risks.append(plan.style_warning)
    if pg and plan.migration and plan.migration.get("required"):
        if evolved.rls:
            plan.risks.append("row-level security: every new table is protected in the additive revision (ENABLE + FORCE, policy, grants to the application role, which must exist and must not bypass RLS); run tests/test_rls.py against the migrated database before relying on it")
        elif evolved.security["multi_tenant"]["strategy"] == "shared_schema_rls":
            plan.risks.append(f"tenant strategy shared_schema_rls is declared in a {evolved.schema.rsplit('/', 1)[-1]} contract and stays unimplemented: the new tables get application scoping only, like the existing ones; adopting RLS is a reviewed design change, not an evolution")
    for m in evolved.modules:
        if m.name in plan.added_modules:
            for e in m.entities:
                for f in e.fields:
                    if f.references and f.references.split(".")[0] in existing:
                        plan.risks.append(f"{m.name}.{e.name}.{f.name} references existing {f.references}: additive foreign key, existing rows unaffected; on_delete={f.on_delete}")
    for c in plan.changes:
        if c["kind"] == "add_field" and "server default" in c["detail"]:
            plan.risks.append(f"{c['target']}: existing rows receive the server default on upgrade; downgrade drops the column and its data")
        elif c["kind"] == "add_field" and "nullable" in c["detail"]:
            plan.risks.append(f"{c['target']}: existing rows read NULL; downgrade drops the column and its data")
    for op in plan.phased_new:
        if op.get("kind") == KIND_RENAME:
            plan.risks.append(f"{op['table']}.{op['from_column']} -> {op['column']} (operation {op['op_id']}): rename in transition: `alembic upgrade head` adds the new column and the sync trigger {op['sync']['trigger']}; run `python -m {evolved.package}.ops.backfill run {op['op_id']}` as the owner to reconcile the rows written before the trigger; the old column stays the source of truth; hand-written SQL that names {op['from_column']!r} keeps working, hand-written SQL that expects {op['column']!r} to be filled must wait for the backfill; nothing is retired or dropped in this version")
            continue
        plan.risks.append(f"{op['table']}.{op['column']} (operation {op['op_id']}): state is declared, not applied: run `alembic upgrade head`, then `python -m {evolved.package}.ops.backfill run {op['op_id']}` as the owner, then `verify --application-version {op['application_version']}`; until the contract phase the column is nullable and the read schema optional; the backfill never overwrites a value and reports `complete` only after counting zero pending rows")
        if evolved.rls:
            plan.risks.append(f"{op['table']}.{op['column']}: the table has row-level security; the runner refuses to run as a role that does not bypass RLS (it would silently see no rows): use the owner URL")
    for i in plan.indexes_new:
        plan.risks.append(f"index {i['name']}: CREATE INDEX CONCURRENTLY in its own revision (SHARE UPDATE EXCLUSIVE: writes continue; two scans; waits for older transactions); an interruption leaves an invalid index that a re-run drops and rebuilds; the revision fails rather than end invalid")
    for op in plan.retiring:
        plan.risks.append(f"{op['table']}.{op['from_column']} -> {op['column']} (operation {op['op_id']}): retire: this generation (C) answers with {op['column']!r} only; clients still sending or reading {op['from_column']!r} must move to B's responses or to the new name; `alembic upgrade head` re-checks the trigger and the equality of the columns, swaps the sync function (truth = {op['column']}), carries NOT NULL and default over and stamps retired_at; the old column stays for B; downgrade: DROP NOT NULL/DEFAULT and the comment back (no data moves)")
    for op in plan.dropping:
        plan.risks.append(f"{op['table']}.{op['from_column']} (operation {op['op_id']}): DESTRUCTIVE: `alembic upgrade head` drops the old column, its sync trigger and function in one transaction after re-checking every condition under ACCESS EXCLUSIVE (lock_timeout 5s); B (and A) fail from that moment; the downgrade recreates the column from the new column's current values offline and cannot restore the dropped content; take a backup before upgrading if the historical content matters; no online rollback")
    for op in plan.contracting:
        plan.risks.append(f"{op['table']}.{op['column']}: contract phase: the revision re-checks pending rows and the progress state on the live database and fails rather than contract prematurely; ADD CONSTRAINT NOT VALID and SET NOT NULL take brief ACCESS EXCLUSIVE locks, VALIDATE takes SHARE UPDATE EXCLUSIVE; an application version that still inserts NULLs makes the revision fail: it must be gone (declared by the operator in the evidence)")
    if plan.preserved_manual:
        plan.risks.append("manual changes are preserved in: " + ", ".join(plan.preserved_manual))
    points = [rel for rel in plan.modify if rel != MANIFEST]
    plan.risks.append("files regenerated from the contract: " + ", ".join(points) if points else "no file regenerated (only the manifest is updated)")
    plan.tests_required = [f"tests/test_{m}.py" for m in plan.added_modules + plan.modified_modules] + (["tests/test_migrations.py"] if pg else []) + (["tests/test_backfill.py"] if phases is not None and (phases.all_phased or phases.new_phased) else []) + ["the existing suite (every module), through the trust model"]
    if plan.blockers:
        plan.state = STATE_BLOCKED
    return plan


# --------------------------------------------------------------------------- #
# adopted projects: anchored structural transforms
# --------------------------------------------------------------------------- #


def _endpoint_code(router: str, ep: Dict[str, Any]) -> str:
    summary = f', summary={json.dumps(ep["summary"])}' if ep.get("summary") else ""
    literal = json.dumps(ep["response"], indent=4, sort_keys=True).replace(": true", ": True").replace(": false", ": False").replace(": null", ": None")
    if literal in ("true", "false", "null"):
        literal = {"true": "True", "false": "False", "null": "None"}[literal]
    body = "\n".join("    " + line for line in f"return {literal}".splitlines())
    return f'@{router}.{ep["method"].lower()}({json.dumps(ep["path"])}{summary})\nasync def {ep["name"]}() -> dict:\n{body}\n'


def _plan_adopted(plan: EvolutionPlan, model: ProjectModel, block) -> EvolutionPlan:
    contract = plan.contract
    project = Path(plan.project)
    adoption = model.adoption or {}
    pre = plan.preconditions
    pre.append(Precondition("adoption_baseline_present", True, f"{len(adoption.get('modules', {}))} adopted module(s)"))
    plan.package = model.package
    plan.style = f"adopted ({model.layout} layout, hand-written)"
    if contract.add_modules:
        block("change_supported_for_adopted", "add_modules generates whole modules; an adopted hand-written project is only transformed inside its adopted modules (modify_modules.add_endpoints / add_fields)")
    modules = adoption.get("modules", {})
    adopted_files: Dict[str, str] = {}
    for data in modules.values():
        adopted_files.update({str(k): str(v) for k, v in (data.get("files") or {}).items()})
    changed = sorted(rel for rel, sha in adopted_files.items() if model.digests.get(rel) != sha)
    if changed:
        block("adopted_files_unchanged_since_adoption", "manual changes after adoption on: " + ", ".join(changed) + " (adopt again to refresh the baseline before transforming them)")
    sources: Dict[str, str] = {}
    edits: Dict[str, List] = {}
    plan.modified_modules = [c.name for c in contract.modify_modules]
    for change in contract.modify_modules:
        data = modules.get(change.name)
        if data is None:
            block("module_is_adopted", f"module {change.name!r} is not adopted (adopted: {', '.join(sorted(modules)) or 'none'}); unadopted files are protected")
            continue
        if change.entities or change.add_depends_on:
            block("change_supported_for_adopted", f"module {change.name!r}: entities[].add_fields / add_operations and add_depends_on describe generated modules; adopted modules accept add_endpoints and add_fields with a class")
            continue
        router_file, router_name = data["router"]["file"], data["router"]["name"]
        module_files = sorted(data.get("files", {}))
        for ep in change.add_endpoints:
            label = f"{change.name}: {ep['method']} {ep['path']} ({ep['name']})"
            rel = router_file
            if rel not in sources:
                sources[rel] = (project / rel).read_text(encoding="utf-8")
            code = _endpoint_code(router_name, ep)
            edits.setdefault(rel, []).append((label, lambda p, s, code=code, ep=ep, router_name=router_name: insert_function(p, s, code, name=ep["name"], router=router_name, method=ep["method"], route_path=ep["path"])))
            plan.changes.append({"kind": "add_endpoint", "target": label, "classification": COMPATIBLE, "detail": "a new handler appended to the adopted router file; existing handlers untouched"})
        for f in change.add_fields:
            label = f"{change.name}: {f['class']}.{f['name']}"
            candidates = [m for m in model.pydantic_models if m["class"] == f["class"] and m["file"] in module_files]
            if not candidates:
                block("field_target_found", f"{label}: no adopted file of module {change.name!r} defines pydantic class {f['class']!r}")
                continue
            if len(candidates) > 1:
                block("field_target_unambiguous", f"{label}: class {f['class']!r} is defined in several adopted files ({', '.join(c['file'] for c in candidates)}); ambiguous target")
                continue
            rel = candidates[0]["file"]
            if rel not in sources:
                sources[rel] = (project / rel).read_text(encoding="utf-8")
            annotation = {"str": "str", "int": "int", "float": "float", "bool": "bool"}[f["type"]]
            if f.get("required") is True:
                line = f"{f['name']}: {annotation} = {json.dumps(f['default']) if f['type'] != 'bool' else repr(bool(f['default']))}"
                detail = "required with a default: existing callers keep working"
            else:
                line = f"{f['name']}: {annotation} | None = None"
                detail = "optional field with None default; existing callers and stored data keep working"
            edits.setdefault(rel, []).append((label, lambda p, s, line=line, f=f: insert_class_field(p, s, f["class"], line, field_name=f["name"])))
            plan.changes.append({"kind": "add_field", "target": label, "classification": COMPATIBLE, "detail": detail})
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    for rel, steps in edits.items():
        try:
            edit: StructuralEdit = compose(rel, sources[rel], [step for _label, step in steps])
        except TransformError as exc:
            block("transform_preconditions", str(exc))
            continue
        plan.modify.append(rel)
        plan.files[rel] = edit.source
        plan.expected[rel] = adopted_files[rel]
        plan.diffs[rel] = edit.diff
        plan.transforms.append({"path": rel, "inserted": edit.inserted, "evidence": edit.evidence, "sha256_before": adopted_files[rel], "sha256_after": hashlib.sha256(edit.source.encode("utf-8")).hexdigest()})
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    if not plan.modify:
        plan.state = STATE_NOOP
        pre.append(Precondition("already_applied", True, "nothing to transform"))
        return plan
    new_adoption = json.loads(json.dumps(adoption))
    for change in contract.modify_modules:
        for rel in new_adoption["modules"][change.name]["files"]:
            if rel in plan.files:
                new_adoption["modules"][change.name]["files"][rel] = hashlib.sha256(plan.files[rel].encode("utf-8")).hexdigest()
        for ep in change.add_endpoints:
            new_adoption["modules"][change.name].setdefault("routes", []).append({"method": ep["method"], "path": ep["path"], "effective_paths": [], "function": ep["name"]})
    new_adoption.setdefault("history", []).append({"operation": "evolve", "contract_digest": contract.digest, "modules": plan.modified_modules, "changes": [c["target"] for c in plan.changes]})
    content = json.dumps(new_adoption, indent=2, sort_keys=True) + "\n"
    plan.modify.append(ADOPTION)
    plan.files[ADOPTION] = content
    plan.expected[ADOPTION] = model.digests.get(ADOPTION)
    plan.public_contracts = {"routes_added": [{"method": ep["method"], "path": ep["path"], "module": c.name} for c in contract.modify_modules for ep in c.add_endpoints], "routes_removed": [], "routes_changed": [], "declared_contracts_changed": False}
    plan.migration = {"required": False, "kind": "not applicable (adopted project; no schema managed)"}
    unadopted = sorted(rel for rel, status in model.ownership.items() if status == "unadopted")
    plan.risks.append(f"{len(unadopted)} unadopted file(s) are protected and will not be written")
    plan.tests_required = ["the project's own suite, through the trust model (authorised by the operator)"]
    return plan


# --------------------------------------------------------------------------- #
# apply and verify
# --------------------------------------------------------------------------- #


@dataclass
class EvolveOptions:
    apply: bool = False
    verify: bool = True
    authorization: Authorization = field(default_factory=Authorization)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    executor: str = "local"
    test_command: Optional[str] = None
    baseline_contract: Optional[str] = None
    keep_copy: bool = False
    faults: Optional[FaultInjector] = None  # tests: interrupt the publication of the real project
    authorize_contract: bool = False  # v2.1: the explicit human authorization a contract phase needs (--authorize-contract)
    evidence: Optional[str] = None  # v2.1: migration evidence file (overrides authorization.contract.evidence)
    confirm_irreversible: Optional[str] = None  # v2.2: the exact phrase a destructive contract prints (`drop <table>.<column>`); distinct from authorize_contract


def _write_plan(target: Path, plan: EvolutionPlan, faults: Optional[FaultInjector] = None, *, durable: bool = True) -> Operation:
    """One journaled operation; its steps (journal 1.1) say what produced each file: regenerated files, structural
    transforms with their evidence digests, and the manifest or adoption baseline."""
    manifest_paths = [p for p in plan.files if p in (MANIFEST, ADOPTION)]
    steps: List[Dict[str, Any]] = []
    if plan.kind == "adopted":
        steps.append({"kind": "transform", "paths": [t["path"] for t in plan.transforms], "evidence": {t["path"]: {"inserted": t["inserted"], "sha256_before": t["sha256_before"], "sha256_after": t["sha256_after"]} for t in plan.transforms}})
    else:
        regenerated = [p for p in sorted(plan.files) if p not in manifest_paths]
        steps.append({"kind": "regenerate", "paths": regenerated, "evidence": {"create": list(plan.create), "modify": [p for p in plan.modify if p not in manifest_paths]}})
    steps.append({"kind": "manifest", "paths": manifest_paths})
    return write_recoverable(target, plan.files, plan.expected, kind="evolve", description=plan.operation_description, faults=faults, durable=durable, steps=steps)


def verify_in_copy(plan: EvolutionPlan, options: EvolveOptions) -> Dict[str, Any]:
    """Apply the plan to a copy of the project and check it: static audit against the project's audit, route
    invariants, protected files, and the tests under the trust model (never without authorization)."""
    project = Path(plan.project)
    workspace = Workspace.create(str(project))
    result: Dict[str, Any] = {"copy": str(workspace.project), "state": STATE_INCONCLUSIVE, "reasons": []}
    try:
        op = _write_plan(workspace.project, plan, durable=False)  # a throwaway copy: no fsync
        result["written_in_copy"] = [f.path for f in op.files]
        before_report = audit_project(str(project))
        after_report = audit_project(str(workspace.project))
        comparison = compare_reports(after_report, before_report, None)
        active_before = [f for f in before_report["findings"] if isinstance(f, dict) and not f.get("suppressed")]
        active_after = [f for f in after_report["findings"] if isinstance(f, dict) and not f.get("suppressed")]
        by_severity: Dict[str, int] = {}
        for f in active_after:
            by_severity[str(f["severity"])] = by_severity.get(str(f["severity"]), 0) + 1
        new_findings = [f for f in comparison.get("new", []) if f.get("severity") in SEVERITY_BLOCKING]
        result["audit"] = {"before": len(active_before), "after": len(active_after), "by_severity_after": dict(sorted(by_severity.items())), "new": comparison.get("new", []), "no_longer_detected": comparison.get("no_longer_detected", []), "blocking_new": new_findings}
        if new_findings:
            result["reasons"].append(f"{len(new_findings)} new high/critical finding(s) in the evolved copy")
        before_routes = {(r["function"], r["method"], r["path"]): r for r in plan.routes_before}
        after_routes = {(c["function"], c["method"], c["path"]): {"effective_paths": c.get("effective_paths", []), "authentication": c["authentication"]} for c in (after_report.get("contracts") or {}).get("routes", [])}
        missing = sorted(k for k in before_routes if k not in after_routes)
        changed = sorted(f"{k[1]} {k[2]} ({k[0]})" for k in before_routes if k in after_routes and (before_routes[k]["effective_paths"], before_routes[k]["authentication"]) != (after_routes[k]["effective_paths"], after_routes[k]["authentication"]))
        result["routes"] = {"before": len(before_routes), "after": len(after_routes), "missing": [f"{k[1]} {k[2]} ({k[0]})" for k in missing], "changed": changed, "added": sorted(f"{k[1]} {k[2]} ({k[0]})" for k in after_routes if k not in before_routes)}
        if (missing or changed) and "existing_routes_unchanged" in plan.contract.invariants:
            result["reasons"].append("existing routes changed or disappeared: " + ", ".join(result["routes"]["missing"] + changed))
        before_digest = dict(plan.discovery.digests) if plan.discovery else {}
        after_digest: Dict[str, str] = {}
        for rel in before_digest:
            path = workspace.project / rel
            if path.is_file():
                after_digest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        touched = sorted(rel for rel in before_digest if rel not in plan.files and after_digest.get(rel) != before_digest[rel])
        result["protected_files"] = {"checked": len(before_digest), "unexpectedly_changed": touched, "preserved_globs": list(plan.contract.preserve)}
        if touched:
            result["reasons"].append("files outside the plan changed in the copy: " + ", ".join(touched[:5]))
        tests: Optional[Dict[str, Any]] = None
        if options.authorization.execution or options.test_command:
            authorization = options.authorization
            if authorization.execution and authorization.covers(str(project)) is None:
                authorization = replace(authorization, scope=str(workspace.project), note=f"scope {options.authorization.scope} covers the project; moved to its verification copy")
            tests = run_generated_tests(str(workspace.project), authorization, options.policy, options.test_command, executor_name=options.executor)
        result["tests"] = tests
        if result["reasons"]:
            result["state"] = STATE_REJECTED
        elif tests is None:
            result["state"] = STATE_AUDIT_CLEAN
            result["reasons"].append("no tests executed (not authorized): audit clean, route invariants hold, protected files untouched")
        elif not tests.get("ran"):
            result["state"] = STATE_INCONCLUSIVE
            result["reasons"].append(f"tests not executed: {tests.get('denied_reason') or (tests.get('run') or {}).get('refused')}")
        else:
            outcome = (tests.get("run") or {}).get("outcome")
            if outcome == "passed":
                result["state"] = STATE_VERIFIED
            elif outcome == "failed":
                result["state"] = STATE_REJECTED
                result["reasons"].append("the test suite of the evolved copy failed")
            else:
                result["state"] = STATE_INCONCLUSIVE
                result["reasons"].append(f"test outcome {outcome!r} is not evidence of a passing suite")
    except (WriteError, GenerationError) as exc:
        result["state"] = STATE_REJECTED
        result["reasons"].append(f"could not apply the plan to the copy: {exc}")
    finally:
        if not options.keep_copy:
            shutil.rmtree(workspace.root, ignore_errors=True)
    return result


def run_evolution(contract_or_path: Any, options: Optional[EvolveOptions] = None) -> Dict[str, Any]:
    options = options or EvolveOptions()
    contract = contract_or_path if isinstance(contract_or_path, EvolutionContract) else load_evolution(contract_or_path)
    plan = plan_evolution(contract, baseline_contract=options.baseline_contract, authorize_contract=options.authorize_contract, evidence_path=options.evidence, confirm_irreversible=options.confirm_irreversible)
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "tool": {"name": "fastapi-architect evolve", "version": TOOL_VERSION},
        "discovery": plan.discovery.to_dict() if plan.discovery else None,
        "plan": plan.to_dict(),
        "verification": None,
        "applied": {"written": [], "state": "not_applied"},
        "authorization": options.authorization.to_dict(),
        "contract_authorization": {"authorize_contract": bool(options.authorize_contract), "confirm_irreversible": options.confirm_irreversible, "approved_by": (contract.authorization.get("destructive") or {}).get("approved_by"), "evidence": options.evidence or (contract.authorization.get("contract") or {}).get("evidence"), "declared_by": (contract.authorization.get("contract") or {}).get("declared_by")},
        "state": plan.state,
        "note": "preview only unless --apply; with --apply the plan is first applied to a copy and verified, and the project is written only when that verification did not reject it, through the recoverable writer (journal under .fastapi-architect/ops); verified requires executed tests, audit_clean means no test was executed",
    }
    if plan.state in (STATE_BLOCKED, STATE_NOOP):
        if plan.state == STATE_NOOP and options.apply:
            report["applied"] = {"written": [], "state": "nothing_to_apply"}
        return report
    if options.verify or options.apply:
        verification = verify_in_copy(plan, options)
        report["verification"] = verification
        report["state"] = verification["state"]
        if options.apply and verification["state"] in (STATE_VERIFIED, STATE_AUDIT_CLEAN):
            try:
                op = _write_plan(Path(plan.project), plan, options.faults)
                report["applied"] = {"written": [f.path for f in op.files], "state": "applied", "operation": op.op_id, "manifest": MANIFEST if plan.kind == "generated" else ADOPTION}
            except LockHeld as exc:
                report["applied"] = {"written": [], "state": "locked", "error": str(exc), "lock": {"holder": dict(exc.holder), "reason": exc.reason}, "retry": "wait for the holder to finish (or `evolve_fastapi.py ops list PROJECT`; `ops unlock` removes only a stale lock)"}
                report["state"] = STATE_REJECTED
            except (WriteError, GenerationError, OSError) as exc:
                pending = pending_operations(Path(plan.project))
                report["applied"] = {"written": [], "state": "write_failed" if not pending else "interrupted", "error": str(exc), "pending_operations": [{"op_id": op.op_id, "state": op.state, "published": list(op.published)} for op in pending], "recover": "evolve_fastapi.py recover PROJECT [--forward]"}
                report["state"] = STATE_REJECTED if not pending else "interrupted"
        elif options.apply:
            report["applied"] = {"written": [], "state": "refused", "reason": f"verification ended {verification['state']}: " + "; ".join(verification["reasons"])}
    return report


def render_text(report: Dict[str, Any]) -> str:
    plan = report["plan"]
    lines = [f"evolution report ({report['tool']['name']} {report['tool']['version']}, {report['schema']}) state: {report['state']}", f"project: {plan['project']} ({plan.get('kind')}) package {plan.get('package')} style {plan.get('style')}", f"baseline {str(plan.get('baseline_spec_digest'))[:12]} -> evolved {str(plan.get('evolved_spec_digest'))[:12]}; adds {', '.join(plan['added_modules']) or '-'}; modifies {', '.join(plan['modified_modules']) or '-'}"]
    for p in plan["preconditions"]:
        lines.append(f"  [{'ok' if p['ok'] else 'FAIL'}] {p['name']}: {p['detail']}")
    for c in plan.get("changes", []):
        lines.append(f"  change {c['classification']}: {c['kind']} {c['target']} ({c['detail']})")
    f = plan["files"]
    lines.append(f"files: create {len(f['create'])}, modify {len(f['modify'])}, unchanged {f['unchanged']}, kept {len(f['kept'])}, preserved manual changes {len(f['preserved_manual_changes'])}")
    for rel in f["create"]:
        lines.append(f"  + {rel}")
    for rel in f["modify"]:
        lines.append(f"  ~ {rel}")
    for rel in f["preserved_manual_changes"]:
        lines.append(f"  = {rel} (manual change preserved)")
    for c in plan["conflicts"]:
        lines.append(f"  ! {c['kind']}: {c['path']} ({c['detail']})")
    for t in plan.get("transforms", []):
        lines.append(f"  transform {t['path']}: inserted {', '.join(t['inserted'])} (sha {t['sha256_before'][:12]} -> {t['sha256_after'][:12]})")
    if plan.get("migration"):
        m = plan["migration"]
        lines.append(f"migration: {'required' if m.get('required') else 'not required'}" + (f" {m.get('new_revision')} after {m.get('head_before')} ({m.get('kind')}; tables {m.get('tables')}; columns {m.get('columns')})" if m.get("required") else f" ({m.get('kind')})"))
    pc = plan.get("public_contracts") or {}
    lines.append(f"public contracts: +{len(pc.get('routes_added', []))} routes, -{len(pc.get('routes_removed', []))}, declared contracts changed: {pc.get('declared_contracts_changed')}")
    for r in plan["risks"]:
        lines.append(f"  risk: {r}")
    v = report.get("verification")
    if v:
        lines.append(f"verification in copy: {v['state']}; audit before {v.get('audit', {}).get('before')} after {v.get('audit', {}).get('after')}, new blocking {len(v.get('audit', {}).get('blocking_new', []))}; routes before {v.get('routes', {}).get('before')} after {v.get('routes', {}).get('after')} missing {len(v.get('routes', {}).get('missing', []))}; protected files unexpectedly changed {len(v.get('protected_files', {}).get('unexpectedly_changed', []))}")
        t = v.get("tests")
        if t:
            run = t.get("run") or {}
            lines.append(f"  tests: {'ran' if t.get('ran') else 'not run'} {run.get('outcome')} {run.get('counts')} ({t.get('denied_reason') or ''})".rstrip(" ()"))
        for r in v.get("reasons", []):
            lines.append(f"  reason: {r}")
    a = report.get("applied") or {}
    lines.append(f"applied: {a.get('state')}" + (f" ({len(a.get('written', []))} files, operation {a.get('operation')})" if a.get("written") else "") + (f": {a.get('reason') or a.get('error')}" if a.get("reason") or a.get("error") else ""))
    if a.get("pending_operations"):
        lines.append(f"  pending: {a['pending_operations']} -> {a.get('recover')}")
    for b in plan["blockers"]:
        lines.append(f"  blocked: {b}")
    return "\n".join(lines) + "\n"


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


__all__ = ["EVOLUTION_SCHEMA", "EVOLUTION_SCHEMAS", "REPORT_SCHEMA", "EvolutionContract", "EvolutionError", "EvolutionPlan", "EvolveOptions", "ModuleChange", "load_evolution", "plan_evolution", "render_text", "run_evolution", "to_json", "validate_evolution", "verify_in_copy"]
