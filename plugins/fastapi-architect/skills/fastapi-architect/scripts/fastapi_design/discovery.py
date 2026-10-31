"""Project discovery (v2.0, increment 3): a conservative, static model of an existing FastAPI project.

Everything here is derived from the files on disk by parsing (``ast``), never by importing or executing the
project. The model reuses the audit engine (``collect_facts`` for routers, mounts and effective prefixes,
``audit_project`` for the route contracts) and the generation manifest when there is one:

* **ownership** per file: ``managed_unchanged`` (listed in ``.fastapi-architect/generation.json`` with the same
  digest), ``managed_modified`` (listed, digest differs: a manual change on a generated file), ``unmanaged`` (not
  listed), ``missing_managed`` (listed, not on disk); without a manifest every file is ``unknown``;
* **layout**: ``layered`` (``api/``, ``services/``, ``repositories/`` under the package), ``modular``
  (``modules/<name>/``), or ``unknown``; the layout is reported, never imposed;
* routers (name, own prefix, effective mount prefixes), routes (method, path, effective paths, authentication),
  pydantic models and settings, SQLAlchemy models (through resolved declarative bases), services, repositories,
  dependency aliases, lifespan, Alembic chain (revisions, heads, historical files changed since generation), the
  declared ``.audit-fastapi.json`` and the baseline contract embedded in a 1.1 manifest.

Whatever cannot be determined is listed under ``uncertainties`` instead of being guessed.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi_audit.discovery import DEFAULT_EXCLUDED_DIRS, iter_python_files
from fastapi_audit.engine import AuditOptions, audit_project
from fastapi_audit.facts import collect_facts, load_config
from fastapi_audit.resolver import Scope, collect_functions, collect_scope, dotted_name, resolve_in_scopes

from .generator import MANIFEST, MANIFEST_SCHEMAS
from .spec import Spec, canonical_digest, validate_spec

PYDANTIC_MODEL_BASES = {"pydantic.BaseModel", "pydantic.main.BaseModel"}
PYDANTIC_SETTINGS_BASES = {"pydantic_settings.BaseSettings", "pydantic.BaseSettings", "pydantic_settings.main.BaseSettings"}
DECLARATIVE_BASES = {"sqlalchemy.orm.DeclarativeBase", "sqlalchemy.orm.decl_api.DeclarativeBase", "sqlalchemy.ext.declarative.declarative_base", "sqlalchemy.orm.declarative_base"}
OWNERSHIP = ("managed_unchanged", "managed_modified", "unmanaged", "missing_managed", "unknown")


@dataclass
class ProjectModel:
    root: str
    package: Optional[str] = None
    source_root: str = ""  # "src" or ""
    layout: str = "unknown"  # layered | modular | unknown
    layout_evidence: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)  # business modules found in the layout
    generated: Optional[Dict[str, Any]] = None  # manifest summary
    baseline_spec: Optional[Spec] = None  # from the manifest's spec_document (1.1)
    manifest: Optional[Dict[str, Any]] = None
    ownership: Dict[str, str] = field(default_factory=dict)  # relpath -> OWNERSHIP
    digests: Dict[str, str] = field(default_factory=dict)  # relpath -> sha256 of the file on disk
    routers: List[Dict[str, Any]] = field(default_factory=list)
    routes: List[Dict[str, Any]] = field(default_factory=list)
    pydantic_models: List[Dict[str, str]] = field(default_factory=list)
    settings: List[Dict[str, str]] = field(default_factory=list)
    sqlalchemy_models: List[Dict[str, str]] = field(default_factory=list)
    services: List[Dict[str, str]] = field(default_factory=list)
    repositories: List[Dict[str, str]] = field(default_factory=list)
    dependencies: List[Dict[str, str]] = field(default_factory=list)
    lifespan: Optional[Dict[str, str]] = None
    alembic: Optional[Dict[str, Any]] = None
    declared_contracts: Optional[Dict[str, Any]] = None
    audit_summary: Optional[Dict[str, Any]] = None
    uncertainties: List[str] = field(default_factory=list)

    @property
    def is_generated(self) -> bool:
        return self.manifest is not None

    def ownership_of(self, relpath: str) -> str:
        return self.ownership.get(relpath, "unknown" if self.manifest is None else "unmanaged")

    def to_dict(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for status in self.ownership.values():
            counts[status] = counts.get(status, 0) + 1
        return {
            "root": self.root,
            "package": self.package,
            "source_root": self.source_root,
            "layout": self.layout,
            "layout_evidence": list(self.layout_evidence),
            "modules": list(self.modules),
            "generated": self.generated,
            "baseline_contract": {"name": self.baseline_spec.name, "schema": self.baseline_spec.schema, "digest": self.baseline_spec.digest, "modules": [m.name for m in self.baseline_spec.modules]} if self.baseline_spec else None,
            "ownership_counts": dict(sorted(counts.items())),
            "ownership": dict(sorted(self.ownership.items())),
            "routers": list(self.routers),
            "routes": list(self.routes),
            "pydantic_models": list(self.pydantic_models),
            "settings": list(self.settings),
            "sqlalchemy_models": list(self.sqlalchemy_models),
            "services": list(self.services),
            "repositories": list(self.repositories),
            "dependencies": list(self.dependencies),
            "lifespan": self.lifespan,
            "alembic": self.alembic,
            "declared_contracts": self.declared_contracts,
            "audit_summary": self.audit_summary,
            "uncertainties": list(self.uncertainties),
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_manifest(root: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    path = root / MANIFEST
    if not path.is_file():
        return None, []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{MANIFEST} is not readable JSON ({exc}); the project is treated as not generated"]
    if not isinstance(manifest, dict) or manifest.get("schema") not in MANIFEST_SCHEMAS:
        return None, [f"{MANIFEST} has an unknown schema {manifest.get('schema') if isinstance(manifest, dict) else None!r}; the project is treated as not generated"]
    if not isinstance(manifest.get("files"), dict):
        return None, [f"{MANIFEST} has no file table; the project is treated as not generated"]
    return manifest, []


def _package_and_source_root(root: Path, manifest: Optional[Dict[str, Any]], notes: List[str]) -> Tuple[Optional[str], str]:
    if manifest and isinstance(manifest.get("package"), str):
        pkg = manifest["package"]
        for source_root in ("src", ""):
            if (root / source_root / pkg / "__init__.py").is_file():
                return pkg, source_root
        notes.append(f"the manifest names package {pkg!r} but src/{pkg}/__init__.py and {pkg}/__init__.py are missing")
    candidates: List[Tuple[str, str]] = []
    for source_root in ("src", ""):
        base = root / source_root if source_root else root
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "__init__.py").is_file() and child.name not in ("tests", "test", "migrations", "alembic") and not child.name.startswith("."):
                candidates.append((child.name, source_root))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        notes.append("no importable package found under src/ or the project root")
        return None, ""
    src_candidates = [c for c in candidates if c[1] == "src"]
    if len(src_candidates) == 1:
        notes.append(f"several packages found ({', '.join(c[0] for c in candidates)}); using the one under src/")
        return src_candidates[0]
    notes.append(f"several packages found ({', '.join(c[0] for c in candidates)}); no single application package can be assumed")
    return None, ""


def _layout(root: Path, package: Optional[str], source_root: str, notes: List[str]) -> Tuple[str, List[str], List[str]]:
    if not package:
        return "unknown", ["no application package"], []
    base = root / source_root / package if source_root else root / package
    evidence: List[str] = []
    modules_dir = base / "modules"
    if modules_dir.is_dir():
        modules = sorted(p.name for p in modules_dir.iterdir() if p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith("_"))
        if modules:
            evidence.append(f"{source_root + '/' if source_root else ''}{package}/modules/ with {len(modules)} module package(s)")
            return "modular", evidence, modules
    layers = [d for d in ("api", "services", "repositories", "schemas", "models") if (base / d).is_dir()]
    if {"api", "services", "repositories"} <= set(layers):
        stems = sorted(p.stem for p in (base / "api").glob("*.py") if p.stem not in ("__init__", "router"))
        evidence.append(f"layer directories {', '.join(layers)} under {package}/")
        return "layered", evidence, stems
    if layers:
        evidence.append(f"only {', '.join(layers)} under {package}/: no complete layer set, no modules/ directory")
    else:
        evidence.append(f"no layer directories and no modules/ directory under {package}/")
    notes.append("the layout is not one the generator produces; evolution cannot assume where a new module belongs")
    return "unknown", evidence, []


def _module_scope(tree: ast.Module, module: str, is_package: bool) -> Scope:
    imports, bound = collect_scope(tree.body, module=module, is_package=is_package)
    return Scope("<module>", False, imports, bound)


def _alembic(root: Path, manifest: Optional[Dict[str, Any]], digests: Dict[str, str], notes: List[str]) -> Optional[Dict[str, Any]]:
    ini = root / "alembic.ini"
    if not ini.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini, encoding="utf-8")
        script_location = parser.get("alembic", "script_location", fallback="migrations")
    except (configparser.Error, OSError) as exc:
        notes.append(f"alembic.ini could not be parsed ({exc})")
        return {"ini": "alembic.ini", "script_location": None, "revisions": [], "heads": [], "chain_ok": False, "historical_modified": []}
    versions = root / script_location / "versions"
    revisions: List[Dict[str, Any]] = []
    if versions.is_dir():
        for path in sorted(versions.glob("*.py")):
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_bytes(), filename=rel)
            except (SyntaxError, ValueError) as exc:
                notes.append(f"{rel} could not be parsed ({exc}); the migration chain is uncertain")
                continue
            values: Dict[str, Any] = {}
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("revision", "down_revision"):
                    v = node.value
                    if isinstance(v, ast.Constant):
                        values[node.targets[0].id] = v.value
                    elif isinstance(v, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Constant) for e in v.elts):
                        values[node.targets[0].id] = [e.value for e in v.elts]
            revision = values.get("revision")
            if not isinstance(revision, str):
                notes.append(f"{rel} has no string revision; the migration chain is uncertain")
                continue
            down = values.get("down_revision")
            revisions.append({"file": rel, "revision": revision, "down_revision": down, "ownership": None})
    known = {r["revision"] for r in revisions}
    referenced: Set[str] = set()
    unknown_parents: List[str] = []
    for r in revisions:
        downs = r["down_revision"] if isinstance(r["down_revision"], list) else ([r["down_revision"]] if r["down_revision"] else [])
        for d in downs:
            referenced.add(d)
            if d not in known:
                unknown_parents.append(f"{r['revision']} -> {d}")
    heads = sorted(r["revision"] for r in revisions if r["revision"] not in referenced)
    historical_modified: List[str] = []
    if manifest:
        for r in revisions:
            expected = manifest["files"].get(r["file"])
            if expected is None:
                r["ownership"] = "unmanaged"
            elif expected == digests.get(r["file"]):
                r["ownership"] = "managed_unchanged"
            else:
                r["ownership"] = "managed_modified"
                historical_modified.append(r["file"])
    chain_ok = len(heads) == 1 and not unknown_parents
    if len(heads) > 1:
        notes.append(f"alembic: {len(heads)} heads ({', '.join(heads)}); the chain must be merged before it can be extended")
    if unknown_parents:
        notes.append("alembic: down_revision points at unknown revisions: " + ", ".join(unknown_parents))
    return {"ini": "alembic.ini", "script_location": script_location, "revisions": revisions, "heads": heads, "chain_ok": chain_ok, "unknown_parents": unknown_parents, "historical_modified": historical_modified}


def discover(project_root: str, *, audit: bool = True) -> ProjectModel:
    """Build the project model. Static only: parses files, reads JSON/INI; executes nothing of the project."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    model = ProjectModel(root=str(root))
    notes = model.uncertainties
    manifest, manifest_notes = _read_manifest(root)
    notes.extend(manifest_notes)
    model.manifest = manifest
    if manifest:
        model.generated = {
            "schema": manifest.get("schema"),
            "tool": manifest.get("tool"),
            "spec": manifest.get("spec"),
            "style": manifest.get("style"),
            "files": len(manifest.get("files", {})),
            "has_spec_document": isinstance(manifest.get("spec_document"), dict),
            "history": manifest.get("history", []),
            "migrations": manifest.get("migrations", []),
        }
        doc = manifest.get("spec_document")
        if isinstance(doc, dict):
            spec, errors = validate_spec(doc, f"{MANIFEST}#spec_document")
            if spec is None:
                notes.append("the manifest's spec_document does not validate with this version: " + "; ".join(errors[:3]))
            elif canonical_digest(doc) != (manifest.get("spec") or {}).get("digest"):
                notes.append("the manifest's spec_document digest does not match manifest.spec.digest; the baseline contract is not trusted")
            else:
                model.baseline_spec = spec
        else:
            notes.append("the manifest has no spec_document (generated before schema 1.1): the baseline contract must be supplied to evolve this project")

    # files and ownership
    files, _skipped = iter_python_files(str(root), excluded_dirs=list(DEFAULT_EXCLUDED_DIRS), include_hidden=False, exclude_tests=False, tests_excluded=[], include_globs=[], exclude_globs=[])
    on_disk: Dict[str, str] = {}
    for rel, full in files:
        try:
            on_disk[rel] = _sha(Path(full).read_bytes())
        except OSError:
            notes.append(f"{rel} could not be read")
    if manifest:
        for rel in manifest["files"]:
            path = root / rel
            if path.is_file():
                try:
                    on_disk.setdefault(rel, _sha(path.read_bytes()))
                except OSError:
                    notes.append(f"{rel} could not be read")
    model.digests = dict(sorted(on_disk.items()))
    for rel, digest in on_disk.items():
        if manifest is None:
            model.ownership[rel] = "unknown"
        elif rel not in manifest["files"]:
            model.ownership[rel] = "unmanaged"
        elif manifest["files"][rel] == digest:
            model.ownership[rel] = "managed_unchanged"
        else:
            model.ownership[rel] = "managed_modified"
    if manifest:
        for rel in manifest["files"]:
            if rel not in on_disk:
                model.ownership[rel] = "missing_managed"
    if manifest is None:
        notes.append("no generation manifest: file ownership is unknown for every file (nothing can be told apart from a manual file)")

    model.package, model.source_root = _package_and_source_root(root, manifest, notes)
    model.layout, model.layout_evidence, model.modules = _layout(root, model.package, model.source_root, notes)

    # facts: routers, mounts, dependency aliases
    config, config_source = load_config(str(root))
    if config_source:
        model.declared_contracts = config
    facts = collect_facts(str(root), files, config=config, config_source=config_source, interprocedural=False)
    seen_routers: Set[str] = set()
    for name, prefix in sorted(facts.router_prefixes.items()):
        short = name.split(".")[-1]
        module = name.rsplit(".", 1)[0]
        rel = facts.module_map.get(module)
        if rel is None or (rel, short) in seen_routers:
            continue
        seen_routers.add((rel, short))
        model.routers.append({"file": rel, "name": short, "prefix": prefix, "effective_prefixes": facts.router_effective_prefixes.get(name, []), "resolution": facts.router_prefix_resolution.get(name, "unmounted")})
    for name in sorted(facts.router_prefix_unknown):
        short = name.split(".")[-1]
        rel = facts.module_map.get(name.rsplit(".", 1)[0])
        if rel and (rel, short) not in seen_routers:
            seen_routers.add((rel, short))
            model.routers.append({"file": rel, "name": short, "prefix": None, "effective_prefixes": [], "resolution": "unresolved"})
            notes.append(f"router {short} in {rel} has a prefix that is not a string constant")
    seen_deps: Set[Tuple[str, str]] = set()
    for alias, target in sorted(facts.dependency_aliases.items()):
        module, _, short = alias.rpartition(".")
        rel = facts.module_map.get(module)
        if rel and (rel, short) not in seen_deps:
            seen_deps.add((rel, short))
            model.dependencies.append({"file": rel, "alias": short, "target": target})

    # classes: pydantic, settings, SQLAlchemy (two passes: declarative bases first), services, repositories, lifespan
    parsed: List[Tuple[str, ast.Module, Scope, str, bool]] = []
    declarative: Set[str] = set(DECLARATIVE_BASES)
    for rel, full in files:
        try:
            tree = ast.parse(Path(full).read_bytes(), filename=rel)
        except (SyntaxError, ValueError, OSError) as exc:
            notes.append(f"{rel} could not be parsed ({exc})")
            continue
        module = facts.file_module.get(rel, "")
        is_package = rel.endswith("__init__.py")
        scope = _module_scope(tree, module, is_package)
        parsed.append((rel, tree, scope, module, is_package))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = {resolve_in_scopes([scope], dotted_name(b) or []) for b in node.bases}
                if bases & DECLARATIVE_BASES:
                    for owner in (module, *[m for m, r in facts.module_map.items() if r == rel]):
                        if owner:
                            declarative.add(f"{owner}.{node.name}")
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                target = resolve_in_scopes([scope], dotted_name(node.value.func) or [])
                if target in DECLARATIVE_BASES:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            for owner in (module, *[m for m, r in facts.module_map.items() if r == rel]):
                                if owner:
                                    declarative.add(f"{owner}.{t.id}")
    for rel, tree, scope, module, is_package in parsed:
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {resolve_in_scopes([scope], dotted_name(b) or []) for b in node.bases}
            local_bases = {f"{module}.{dotted_name(b)[0]}" for b in node.bases if dotted_name(b) and len(dotted_name(b)) == 1 and module}
            if bases & PYDANTIC_SETTINGS_BASES:
                model.settings.append({"file": rel, "class": node.name})
            elif bases & PYDANTIC_MODEL_BASES:
                model.pydantic_models.append({"file": rel, "class": node.name})
            elif bases & DECLARATIVE_BASES:
                continue  # the declarative base itself, not a model
            elif (bases | local_bases) & declarative:
                table = None
                for item in node.body:
                    if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets) and isinstance(item.value, ast.Constant):
                        table = item.value.value
                model.sqlalchemy_models.append({"file": rel, "class": node.name, "table": table})
            elif node.name.endswith("Service"):
                model.services.append({"file": rel, "class": node.name})
            elif node.name.endswith(("Repository", "Repo")):
                model.repositories.append({"file": rel, "class": node.name})
            elif bases and None in bases and not local_bases:
                notes.append(f"{rel}: base class of {node.name} could not be resolved")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and resolve_in_scopes([scope], dotted_name(node.func) or []) in {"fastapi.FastAPI", "fastapi.applications.FastAPI"}:
                for kw in node.keywords:
                    if kw.arg == "lifespan":
                        parts = dotted_name(kw.value)
                        model.lifespan = {"file": rel, "function": ".".join(parts) if parts else "<expression>", "line": getattr(node, "lineno", 0)}

    # routes from the audit contracts (methods, effective paths, authentication)
    if audit:
        try:
            report = audit_project(str(root), options=AuditOptions())
            routes = (report.get("contracts") or {}).get("routes", [])
            model.routes = [
                {"file": c["file"], "function": c["function"], "method": c["method"], "path": c["path"], "effective_paths": c.get("effective_paths", []), "path_resolution": c.get("path_resolution"), "authentication": c["authentication"], "public": c["public"]}
                for c in routes
            ]
            active = [f for f in report["findings"] if isinstance(f, dict) and not f.get("suppressed")]
            by_rule: Dict[str, int] = {}
            for f in active:
                by_rule[str(f["rule"])] = by_rule.get(str(f["rule"]), 0) + 1
            model.audit_summary = {"findings_total": len(active), "by_rule": dict(sorted(by_rule.items())), "contracts": (report.get("contracts") or {}).get("summary")}
        except Exception as exc:  # the audit is a courtesy of discovery; its failure is an uncertainty, not a crash
            notes.append(f"audit of the project failed: {exc}")
    model.alembic = _alembic(root, manifest, model.digests, notes)
    return model
