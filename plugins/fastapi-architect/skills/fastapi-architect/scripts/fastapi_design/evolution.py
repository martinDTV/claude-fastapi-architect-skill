"""Safe project evolution (v2.0, increment 3): extend a generated project with new modules, under a contract.

Flow: ``discover`` the project → validate the **evolution contract** (``fastapi-architect/evolution/1.0``) →
merge the new modules into the baseline contract stored in the manifest → re-validate the whole contract (cycles,
references, duplicates, reserved names) → rebuild the blueprint with the baseline style and the migration lineage
→ compare it with the baseline blueprint and with the files on disk → a **plan** (create, modify, unchanged,
preserved manual changes, conflicts, preconditions, public-contract changes, migration, risks, tests) → optionally
**apply** it: first to a copy where the audit, the route invariants, the protected files and (under the v1.9 trust
model) the tests are checked, then, only if that verification did not reject it, to the project itself with the
two-phase writer that re-checks every digest.

Ownership rules: a file is managed only when the manifest lists it; a managed file whose content differs from the
manifest is a manual change; a file this evolution does not need to change is never written, whatever its state;
an integration point (the files the generator regenerates to mount a module) is modified only when it is
managed, unchanged, allowed by the contract and not preserved. Anything else is a conflict and blocks the plan.
Nothing is deleted. Text replacement is never used: every written file is regenerated from the contract.
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

from fastapi_audit.model import TOOL_VERSION
from fastapi_audit.remediate.executors import Authorization
from fastapi_audit.remediate.isolation import ExecutionPolicy, Workspace
from fastapi_audit.report import compare_reports

from .advisor import recommend
from .blueprint import Lineage, build
from .discovery import ProjectModel, discover
from .generator import MANIFEST, GenerationError, WriteError, build_manifest, safe_join, write_files
from .spec import IDENT_RE, Spec, SpecError, canonical_digest, validate_spec
from fastapi_audit.engine import audit_project

from .verify import run_generated_tests

EVOLUTION_SCHEMA = "fastapi-architect/evolution/1.0"
REPORT_SCHEMA = "evolution_report/1.0"
INVARIANTS = ("existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged")
DEFAULT_INTEGRATION_POINTS = ("pyproject.toml", "README.md", ".env.example", ".audit-fastapi.json", "src/{pkg}/main.py", "src/{pkg}/api/router.py", "src/{pkg}/core/config.py", "migrations/env.py", "tests/conftest.py", "tests/test_migrations.py")
STATE_PLANNED = "planned"
STATE_NOOP = "noop"  # every requested module is already present with the same definition: nothing to do
STATE_BLOCKED = "blocked"
STATE_VERIFIED = "verified"
STATE_AUDIT_CLEAN = "audit_clean"
STATE_INCONCLUSIVE = "inconclusive"
STATE_REJECTED = "rejected"
SEVERITY_BLOCKING = ("high", "critical")


class EvolutionError(RuntimeError):
    def __init__(self, source: Optional[str], errors: List[str]) -> None:
        self.source = source or "<evolution>"
        self.errors = list(errors)
        super().__init__(f"{self.source}: {len(self.errors)} contract error(s): " + "; ".join(self.errors))


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #


@dataclass
class EvolutionContract:
    schema: str
    project_path: str
    package: Optional[str]
    expected_spec_digest: Optional[str]
    baseline_contract: Optional[str]
    add_modules: List[Dict[str, Any]]
    invariants: List[str]
    allow_modify: Optional[List[str]]  # globs; None = the default integration points
    preserve: List[str]
    source: Optional[str] = None
    document: Dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return canonical_digest(self.document)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.schema, "project_path": self.project_path, "package": self.package, "expected_spec_digest": self.expected_spec_digest, "baseline_contract": self.baseline_contract, "add_modules": [m.get("name") for m in self.add_modules], "invariants": list(self.invariants), "allow_modify": self.allow_modify, "preserve": list(self.preserve), "digest": self.digest}


def validate_evolution(document: Any, source: Optional[str] = None) -> Tuple[Optional[EvolutionContract], List[str]]:
    errors: List[str] = []
    if not isinstance(document, dict):
        return None, ["top level: expected an object"]
    known = ("schema", "project", "add_modules", "invariants", "files")
    for key in document:
        if key not in known and not str(key).startswith("x-"):
            errors.append(f"evolution.{key}: unknown property (known: {', '.join(known)})")
    if document.get("schema") != EVOLUTION_SCHEMA:
        errors.append(f"evolution.schema: expected {EVOLUTION_SCHEMA!r}, got {document.get('schema')!r}")
    project = document.get("project")
    path = package = expected = baseline = None
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
    modules = document.get("add_modules")
    if not isinstance(modules, list) or not modules:
        errors.append("evolution.add_modules: expected a non-empty list of modules (same shape as spec.functional.modules)")
        modules = []
    for i, m in enumerate(modules):
        if not isinstance(m, dict):
            errors.append(f"evolution.add_modules[{i}]: expected an object")
        elif not isinstance(m.get("name"), str) or not IDENT_RE.match(m.get("name", "")):
            errors.append(f"evolution.add_modules[{i}].name: module name must be a lowercase python identifier")
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
    return EvolutionContract(schema=EVOLUTION_SCHEMA, project_path=str(path), package=package, expected_spec_digest=expected, baseline_contract=baseline, add_modules=modules, invariants=list(invariants), allow_modify=allow_modify, preserve=preserve, source=source, document=json.loads(json.dumps(document))), []


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
    package: Optional[str] = None
    style: Optional[str] = None
    baseline_spec_digest: Optional[str] = None
    evolved_spec_digest: Optional[str] = None
    added_modules: List[str] = field(default_factory=list)
    preconditions: List[Precondition] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    create: List[str] = field(default_factory=list)
    modify: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)  # historical migrations kept verbatim
    preserved_manual: List[str] = field(default_factory=list)  # managed files with manual changes that this evolution does not need to touch
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    public_contracts: Dict[str, Any] = field(default_factory=dict)
    migration: Dict[str, Any] = field(default_factory=dict)
    style_warning: Optional[str] = None
    risks: List[str] = field(default_factory=list)
    tests_required: List[str] = field(default_factory=list)
    diffs: Dict[str, str] = field(default_factory=dict)  # modified file -> unified diff
    files: Dict[str, str] = field(default_factory=dict)  # content to write (create + modify + manifest)
    expected: Dict[str, Optional[str]] = field(default_factory=dict)  # precondition digests for the writer
    evolved_spec: Optional[Spec] = None
    discovery: Optional[ProjectModel] = None
    routes_before: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "project": self.project,
            "contract": self.contract.to_dict(),
            "package": self.package,
            "style": self.style,
            "baseline_spec_digest": self.baseline_spec_digest,
            "evolved_spec_digest": self.evolved_spec_digest,
            "added_modules": list(self.added_modules),
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
    raw = Path(contract.project_path)
    if not raw.is_absolute() and contract.source:
        raw = Path(contract.source).resolve().parent / raw
    return raw


def plan_evolution(contract: EvolutionContract, *, baseline_contract: Optional[str] = None, tool_version: str = TOOL_VERSION) -> EvolutionPlan:
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
    model = discover(str(project))
    plan.discovery = model
    plan.routes_before = list(model.routes)
    pre.append(Precondition("project_is_not_a_symlink", True, str(project)))

    # baseline contract
    if model.manifest is None:
        block("generation_manifest_present", "no .fastapi-architect/generation.json: the project was not generated by this tool (or the manifest was removed); evolving hand-written projects is not supported in this increment")
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

    # idempotency: the same contract applied again is a no-op, a different definition of an existing module is a conflict
    existing = [m.name for m in baseline.modules]
    baseline_modules = {m.get("name"): m for m in baseline.document.get("functional", {}).get("modules", []) if isinstance(m, dict)}
    already = [m for m in contract.add_modules if m.get("name") in baseline_modules]
    if already and len(already) == len(contract.add_modules) and all(json.dumps(baseline_modules[m["name"]], sort_keys=True) == json.dumps(m, sort_keys=True) for m in already):
        plan.state = STATE_NOOP
        plan.evolved_spec_digest = baseline.digest
        plan.added_modules = []
        pre.append(Precondition("already_applied", True, "every requested module exists with the same definition; nothing to do"))
        plan.public_contracts = {"routes_added": [], "routes_removed": [], "routes_changed": [], "declared_contracts_changed": False}
        plan.migration = {"required": False, "kind": "none (already applied)"}
        return plan
    for m in already:
        if json.dumps(baseline_modules[m["name"]], sort_keys=True) != json.dumps(m, sort_keys=True):
            block("module_is_new", f"module {m.get('name')!r} already exists with a different definition; changing an existing module is not supported in this increment")
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan

    # merged contract
    offset = len(existing)
    evolved_doc = json.loads(json.dumps(baseline.document))
    evolved_doc.setdefault("functional", {}).setdefault("modules", [])
    evolved_doc["functional"]["modules"] = list(evolved_doc["functional"]["modules"]) + list(contract.add_modules)
    evolved, errors = validate_spec(evolved_doc, "evolved contract")
    if errors:
        mapped = []
        for e in errors:
            match = re.match(r"spec\.functional\.modules\[(\d+)\](.*)", e)
            if match and int(match.group(1)) >= offset:
                mapped.append(f"evolution.add_modules[{int(match.group(1)) - offset}]{match.group(2)}")
            else:
                mapped.append(e)
        block("merged_contract_valid", "; ".join(mapped))
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan
    assert evolved is not None
    # route collisions: an entity collection name is a route; it must be unique across modules
    plural_owner: Dict[str, str] = {}
    for m in evolved.modules:
        for e in m.entities:
            if e.plural in plural_owner and plural_owner[e.plural] != m.name:
                block("routes_are_unique", f"entity {e.name!r} of module {m.name!r} would mount /{e.plural}, already used by module {plural_owner[e.plural]!r}")
            plural_owner.setdefault(e.plural, m.name)
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan

    # style: the existing structure is kept; the advisor's new opinion is reported, never applied
    rec = recommend(evolved)
    if rec.style != plan.style:
        plan.style_warning = f"the advisor would now recommend '{rec.style}' for {len(evolved.modules)} modules; the project keeps its '{plan.style}' layout (migrating the layout is a separate, explicit operation)"
        evolved_doc.setdefault("architecture", {})["style_preference"] = plan.style
        evolved, errors = validate_spec(evolved_doc, "evolved contract")
        assert evolved is not None and not errors, errors
        rec = recommend(evolved)
    plan.evolved_spec = evolved
    plan.evolved_spec_digest = evolved.digest
    plan.added_modules = [m.get("name") for m in contract.add_modules]

    # migrations
    pg = evolved.persistence["kind"] == "postgresql"
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
            lineage = Lineage(head=chain[-1]["revision"], files=[entry["file"] for entry in chain], tables=tables, revisions=list(chain))
            pre.append(Precondition("alembic_chain_is_linear", True, f"head {lineage.head}, {len(chain)} revision(s)"))
    if plan.blockers:
        plan.state = STATE_BLOCKED
        return plan

    # blueprints: baseline (what the files should be now) and evolved (what they should become)
    baseline_rec = recommend(baseline)
    old_files = build(baseline, baseline_rec).files  # what every managed file should contain right now
    new_bp = build(evolved, rec, lineage)
    new_files = dict(new_bp.files)
    history = list(model.manifest.get("history") or [{"operation": "generate", "spec_digest": baseline.digest, "modules": existing}])
    history.append({"operation": "evolve", "contract_digest": contract.digest, "previous_spec_digest": baseline.digest, "spec_digest": evolved.digest, "added_modules": plan.added_modules})
    manifest = build_manifest(evolved, new_bp, tool_version, history=history)
    if lineage:
        for rel in lineage.files:  # kept verbatim: the manifest keeps their digests
            manifest["files"][rel] = model.manifest["files"].get(rel) or model.digests.get(rel)
    new_files[MANIFEST] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    old_files[MANIFEST] = json.dumps(model.manifest, indent=2, sort_keys=True) + "\n"
    allow = contract.allow_modify if contract.allow_modify is not None else [p.format(pkg=baseline.package) for p in DEFAULT_INTEGRATION_POINTS]
    allow.append(MANIFEST)
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
        if rel == MANIFEST:  # the manifest records every operation; it is always rewritten against its current digest
            current = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            plan.modify.append(rel)
            plan.files[rel] = content
            plan.expected[rel] = current
            continue
        if rel not in old_files:  # a new file
            if on_disk is not None:
                plan.conflicts.append({"path": rel, "kind": "file_in_the_way", "detail": f"a {status} file already exists where a new file would be created"})
            else:
                plan.create.append(rel)
                plan.files[rel] = content
                plan.expected[rel] = None
            continue
        if content == old_files[rel]:  # this evolution does not change the file
            if status == "managed_modified":
                plan.preserved_manual.append(rel)
            plan.unchanged.append(rel)
            continue
        # the file changes: integration point rules
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
        old_text = (project / rel).read_text(encoding="utf-8", errors="replace") if rel != MANIFEST else old_files[rel]
        diff = difflib.unified_diff(old_text.splitlines(), content.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=2)
        plan.diffs[rel] = "\n".join(diff)[:20000]
    if lineage:
        plan.kept = list(lineage.files)
    for rel, status in sorted(model.ownership.items()):
        if status == "managed_modified" and rel not in plan.preserved_manual and rel not in plan.modify and rel not in [c["path"] for c in plan.conflicts]:
            plan.preserved_manual.append(rel)
    if plan.conflicts:
        for c in plan.conflicts:
            plan.blockers.append(f"{c['kind']}: {c['path']}")

    # public contracts
    before = _routes_of(baseline, existing)
    after = _routes_of(evolved, [m.name for m in evolved.modules])
    added = [r for r in after if r not in before]
    removed = [r for r in before if r not in after]
    plan.public_contracts = {"routes_added": added, "routes_removed": removed, "routes_changed": [], "declared_contracts_changed": new_files.get(".audit-fastapi.json") != old_files.get(".audit-fastapi.json")}
    if removed and "existing_routes_unchanged" in contract.invariants:
        block("existing_routes_unchanged", f"{len(removed)} existing route(s) would disappear")

    # migration summary
    if pg and lineage:
        delta = [m for m in new_bp.migrations if m["revision"] != lineage.head and m["revision"] not in {r["revision"] for r in lineage.revisions}]
        plan.migration = {"required": bool(delta), "head_before": lineage.head, "new_revision": delta[0]["revision"] if delta else None, "file": delta[0]["file"] if delta else None, "tables": delta[0]["tables"] if delta else [], "kind": "additive (create_table, foreign keys to existing tables, indexes); no drop, no alter" if delta else "none", "historical_files_kept": list(lineage.files)}
    else:
        plan.migration = {"required": False, "kind": "not applicable (in-memory persistence)" if not pg else "unknown"}

    # risks and tests
    if plan.style_warning:
        plan.risks.append(plan.style_warning)
    for m in evolved.modules:
        if m.name in plan.added_modules:
            for e in m.entities:
                for f in e.fields:
                    if f.references and f.references.split(".")[0] in existing:
                        plan.risks.append(f"{m.name}.{e.name}.{f.name} references existing {f.references}: additive foreign key, existing rows unaffected; on_delete={f.on_delete}")
    if plan.preserved_manual:
        plan.risks.append("manual changes are preserved in: " + ", ".join(plan.preserved_manual) + " (the manifest keeps the generated digests; discovery will keep reporting them as managed_modified)")
    points = [rel for rel in plan.modify if rel != MANIFEST]
    plan.risks.append("integration points regenerated from the contract: " + ", ".join(points) if points else "no integration point changes (only the manifest is updated)")
    plan.tests_required = [f"tests/test_{m}.py" for m in plan.added_modules] + (["tests/test_migrations.py"] if pg else []) + ["the existing suite (every module), through the trust model"]
    if plan.blockers:
        plan.state = STATE_BLOCKED
    return plan


# --------------------------------------------------------------------------- #
# apply and verify
# --------------------------------------------------------------------------- #


@dataclass
class EvolveOptions:
    apply: bool = False  # write to the project after a successful verification in a copy
    verify: bool = True
    authorization: Authorization = field(default_factory=Authorization)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    executor: str = "local"
    test_command: Optional[str] = None
    baseline_contract: Optional[str] = None
    keep_copy: bool = False


def _write_plan(target: Path, plan: EvolutionPlan) -> List[str]:
    return write_files(target, plan.files, plan.expected)


def verify_in_copy(plan: EvolutionPlan, options: EvolveOptions) -> Dict[str, Any]:
    """Apply the plan to a copy of the project and check it: static audit against the baseline audit, route
    invariants, protected files, and the tests under the trust model (never without authorization)."""
    project = Path(plan.project)
    workspace = Workspace.create(str(project))
    result: Dict[str, Any] = {"copy": str(workspace.project), "state": STATE_INCONCLUSIVE, "reasons": []}
    try:
        written = _write_plan(workspace.project, plan)
        result["written_in_copy"] = written
        # static audit of the copy, compared by fingerprint with the audit of the project as it is
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
        # route invariants: every route of the baseline is still there, same method/path/effective paths/authentication
        before_routes = {(r["function"], r["method"], r["path"]): r for r in plan.routes_before}
        after_routes = {(r["function"], r["method"], r["path"]): r for r in [{"function": c["function"], "method": c["method"], "path": c["path"], "effective_paths": c.get("effective_paths", []), "authentication": c["authentication"]} for c in (after_report.get("contracts") or {}).get("routes", [])]}
        missing = sorted(k for k in before_routes if k not in after_routes)
        changed = sorted(f"{k[1]} {k[2]} ({k[0]})" for k in before_routes if k in after_routes and (before_routes[k]["effective_paths"], before_routes[k]["authentication"]) != (after_routes[k]["effective_paths"], after_routes[k]["authentication"]))
        result["routes"] = {"before": len(before_routes), "after": len(after_routes), "missing": [f"{k[1]} {k[2]} ({k[0]})" for k in missing], "changed": changed, "added": sorted(f"{k[1]} {k[2]} ({k[0]})" for k in after_routes if k not in before_routes)}
        if (missing or changed) and "existing_routes_unchanged" in plan.contract.invariants:
            result["reasons"].append("existing routes changed or disappeared: " + ", ".join(result["routes"]["missing"] + changed))
        # protected files: nothing outside the plan changed in the copy
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
        # tests under the trust model: the operator authorises the project; the copy is where it runs. The scope is
        # moved to the copy only when it covers the project (anything else is denied exactly as before)
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
            elif outcome in ("failed",):
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
    plan = plan_evolution(contract, baseline_contract=options.baseline_contract)
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "tool": {"name": "fastapi-architect evolve", "version": TOOL_VERSION},
        "discovery": plan.discovery.to_dict() if plan.discovery else None,
        "plan": plan.to_dict(),
        "verification": None,
        "applied": {"written": [], "state": "not_applied"},
        "authorization": options.authorization.to_dict(),
        "state": plan.state,
        "note": "preview only unless --apply; with --apply the plan is first applied to a copy and verified, and the project is written only when that verification did not reject it; verified requires executed tests, audit_clean means no test was executed",
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
                written = _write_plan(Path(plan.project), plan)
                report["applied"] = {"written": written, "state": "applied", "manifest": MANIFEST}
            except (WriteError, GenerationError) as exc:
                report["applied"] = {"written": [], "state": "write_failed", "error": str(exc)}
                report["state"] = STATE_REJECTED
        elif options.apply:
            report["applied"] = {"written": [], "state": "refused", "reason": f"verification ended {verification['state']}: " + "; ".join(verification["reasons"])}
    return report


def render_text(report: Dict[str, Any]) -> str:
    plan = report["plan"]
    lines = [f"evolution report ({report['tool']['name']} {report['tool']['version']}, {report['schema']}) state: {report['state']}", f"project: {plan['project']} package {plan.get('package')} style {plan.get('style')}", f"baseline {str(plan.get('baseline_spec_digest'))[:12]} -> evolved {str(plan.get('evolved_spec_digest'))[:12]}; adds {', '.join(plan['added_modules']) or '-'}"]
    for p in plan["preconditions"]:
        lines.append(f"  [{'ok' if p['ok'] else 'FAIL'}] {p['name']}: {p['detail']}")
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
    if plan.get("migration"):
        m = plan["migration"]
        lines.append(f"migration: {'required' if m.get('required') else 'not required'}" + (f" {m.get('new_revision')} after {m.get('head_before')} ({m.get('kind')}; tables {m.get('tables')})" if m.get("required") else f" ({m.get('kind')})"))
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
    lines.append(f"applied: {a.get('state')}" + (f" ({len(a.get('written', []))} files)" if a.get("written") else "") + (f": {a.get('reason') or a.get('error')}" if a.get("reason") or a.get("error") else ""))
    for b in plan["blockers"]:
        lines.append(f"  blocked: {b}")
    return "\n".join(lines) + "\n"


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


__all__ = ["EVOLUTION_SCHEMA", "REPORT_SCHEMA", "EvolutionContract", "EvolutionError", "EvolutionPlan", "EvolveOptions", "load_evolution", "plan_evolution", "render_text", "run_evolution", "to_json", "validate_evolution", "verify_in_copy"]
