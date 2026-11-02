"""Selective adoption of hand-written projects (v2.0, increment 4).

A project without a generation manifest cannot be regenerated, so nothing in it is managed. Adoption lets the
operator declare, module by module, which files FastAPI Architect may transform from now on:

1. ``propose_adoption`` reads the discovery model and lists candidate modules (one per router file, with the
   project files that router imports as related files), the files nobody claims, and the uncertainties;
2. the operator writes an **adoption contract** (``fastapi-architect/adoption/1.0``) naming the modules and the
   exact files of each one; nothing is adopted by default and no file can belong to two modules;
3. ``adopt`` validates the contract against the project (files exist, are inside it, are not symlinks, parse, the
   router is bound exactly once), refuses generated projects and unknown structures, and records the baseline in
   ``.fastapi-architect/adoption.json``: the adopted files with their sha256, the router of each module, its routes,
   and the ownership boundary (every other file stays protected). The manifest is written through the recoverable
   writer; it is the only file touched.

Adoption never converts the project: no file is rewritten, no layout is imposed. Afterwards the evolution engine
can apply the conservative structural transforms of ``transforms.py`` to adopted files only, and a manual change on
an adopted file (digest differs from the adoption baseline) blocks any transform until the baseline is refreshed by
adopting again.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi_audit.model import TOOL_VERSION
from fastapi_common.fs import resolve_against

from .adopted import alembic_facts, mount_candidates, resolve_mount, resolve_symbol, validate_adoption_extensions
from .discovery import ProjectModel, discover
from .generator import GenerationError, WriteError, safe_join
from .journal import FaultInjector, sha256_bytes, write_recoverable
from .spec import IDENT_RE
from .transforms import TransformError, module_names

ADOPTION = ".fastapi-architect/adoption.json"
ADOPTION_SCHEMA = "fastapi-architect/adoption/1.0"
ADOPTION_SCHEMA_11 = "fastapi-architect/adoption/1.1"  # v2.2 inc. 4: package, mount, new_modules, dependencies, operations, unmanaged, alembic
ADOPTION_SCHEMAS = (ADOPTION_SCHEMA, ADOPTION_SCHEMA_11)


class AdoptionError(RuntimeError):
    def __init__(self, source: Optional[str], errors: List[str]) -> None:
        self.source = source or "<adoption>"
        self.errors = list(errors)
        super().__init__(f"{self.source}: {len(self.errors)} adoption error(s): " + "; ".join(self.errors))


@dataclass
class AdoptedModule:
    name: str
    router_file: str
    router_name: str
    files: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "router": {"file": self.router_file, "name": self.router_name}, "files": list(self.files)}


@dataclass
class AdoptionContract:
    project_path: str
    modules: List[AdoptedModule]
    notes: Optional[str] = None
    source: Optional[str] = None
    document: Dict[str, Any] = field(default_factory=dict)
    schema: str = ADOPTION_SCHEMA
    extensions: Dict[str, Any] = field(default_factory=dict)  # 1.1: package, mount, new_modules, dependencies, operations, unmanaged, alembic

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": self.schema, "project_path": self.project_path, "modules": [m.to_dict() for m in self.modules], "notes": self.notes, **({"extensions": self.extensions} if self.extensions else {})}


def propose_adoption(model: ProjectModel) -> Dict[str, Any]:
    """Candidates for the operator to choose from; nothing is decided here."""
    root = Path(model.root)
    claimed: Dict[str, str] = {}
    candidates: List[Dict[str, Any]] = []
    routes_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for r in model.routes:
        routes_by_file.setdefault(r["file"], []).append(r)
    for router in model.routers:
        rel = router["file"]
        stem = Path(rel).stem if Path(rel).stem != "__init__" else Path(rel).parent.name
        name = re.sub(r"[^a-z0-9_]", "_", stem.lower())
        related: List[str] = []
        try:
            tree = ast.parse((root / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError, OSError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for candidate_rel in model.digests:
                        if candidate_rel.endswith(".py") and candidate_rel[:-3].replace("/", ".").endswith(node.module) and candidate_rel != rel and not candidate_rel.startswith("tests/"):
                            related.append(candidate_rel)
        candidates.append({
            "name": name,
            "router": {"file": rel, "name": router["name"], "prefix": router["prefix"], "effective_prefixes": router["effective_prefixes"], "resolution": router["resolution"]},
            "routes": [{"method": x["method"], "path": x["path"], "effective_paths": x["effective_paths"], "function": x["function"]} for x in routes_by_file.get(rel, [])],
            "files": [rel],
            "related_files": sorted(set(related)),
            "ownership": model.ownership_of(rel),
        })
        claimed[rel] = name
    unassigned = sorted(p for p in model.digests if p.endswith(".py") and p not in claimed)
    mounts = mount_candidates(root, model)
    unique_mount = mounts[0] if len(mounts) == 1 and not mounts[0]["dynamic_registrations"] and mounts[0]["app_bound_here"] else None
    alembic = model.alembic or {}
    skeleton: Dict[str, Any] = {
        "schema": ADOPTION_SCHEMA_11,
        "project": {"path": model.root},
        "modules": [{"name": c["name"], "router": {"file": c["router"]["file"], "name": c["router"]["name"]}, "files": c["files"] + c["related_files"]} for c in candidates],
        "package": model.package,
        "unmanaged": [],
        "mount": {"file": unique_mount["file"], "function": unique_mount["function"], "app": unique_mount["app"], "after_call": unique_mount["after_call"], "prefix": ""} if unique_mount else None,
        "new_modules": {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": f"{model.package}.main" if model.package else "app.main", "attribute": "app"}},
        "dependencies": {"allowed": []},
        "operations": [],
        "notes": "COMPLETE AND APPROVE: nothing here is a permission until the operator writes it; mount.prefix, unmanaged, dependencies.allowed, operations and alembic.metadata/session_dependency are deliberately empty or null",
    }
    if alembic.get("script_location"):
        skeleton["alembic"] = {"ini": "alembic.ini", "script_location": alembic.get("script_location"), "metadata": None, "session_dependency": None, "async": True}
    return {
        "project": model.root,
        "package": model.package,
        "layout": model.layout,
        "generated": model.is_generated,
        "already_adopted": sorted((model.adoption or {}).get("modules", {}).keys()) if getattr(model, "adoption", None) else [],
        "candidates": candidates,
        "mount_candidates": mounts,
        "alembic": {"script_location": alembic.get("script_location"), "heads": alembic.get("heads"), "revisions": len(alembic.get("revisions") or []), "unknown_parents": alembic.get("unknown_parents")} if alembic else None,
        "adoption_1_1_skeleton": skeleton,
        "unassigned_files": unassigned,
        "uncertainties": list(model.uncertainties),
        "note": "nothing is adopted by this proposal: write an adoption contract naming the modules and the exact files to adopt; every other file stays protected",
    }


def validate_adoption(document: Any, source: Optional[str] = None) -> Tuple[Optional[AdoptionContract], List[str]]:
    errors: List[str] = []
    if not isinstance(document, dict):
        return None, ["top level: expected an object"]
    schema = document.get("schema")
    extension_keys = ("package", "unmanaged", "mount", "new_modules", "dependencies", "operations", "alembic")
    for key in document:
        if key in extension_keys:
            if schema != ADOPTION_SCHEMA_11:
                errors.append(f"adoption.{key}: needs schema {ADOPTION_SCHEMA_11}")
        elif key not in ("schema", "project", "modules", "notes") and not str(key).startswith("x-"):
            errors.append(f"adoption.{key}: unknown property")
    if schema not in ADOPTION_SCHEMAS:
        errors.append(f"adoption.schema: expected one of {', '.join(ADOPTION_SCHEMAS)}, got {schema!r}")
    extensions: Dict[str, Any] = {}
    if schema == ADOPTION_SCHEMA_11:
        extensions, extension_errors = validate_adoption_extensions(document)
        errors.extend(extension_errors)
    project = document.get("project") if isinstance(document.get("project"), dict) else {}
    path = project.get("path")
    if not isinstance(path, str) or not path.strip():
        errors.append("adoption.project.path: required non-empty string")
    modules_raw = document.get("modules")
    modules: List[AdoptedModule] = []
    if modules_raw is None and schema == ADOPTION_SCHEMA_11 and extensions.get("mount"):
        modules_raw = []  # 1.1: a project may be adopted for new modules only (nothing existing transformed)
    if not isinstance(modules_raw, list) or (not modules_raw and not (schema == ADOPTION_SCHEMA_11 and extensions.get("mount"))):
        errors.append("adoption.modules: expected a non-empty list (or, under 1.1, a mount for new modules)")
        modules_raw = []
    seen_names: set = set()
    seen_files: Dict[str, str] = {}
    for i, m in enumerate(modules_raw):
        p = f"adoption.modules[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{p}: expected an object")
            continue
        for key in m:
            if key not in ("name", "router", "files") and not str(key).startswith("x-"):
                errors.append(f"{p}.{key}: unknown property")
        name = m.get("name")
        if not isinstance(name, str) or not IDENT_RE.match(name):
            errors.append(f"{p}.name: module name must be a lowercase python identifier")
            name = None
        elif name in seen_names:
            errors.append(f"{p}.name: duplicate module {name!r}")
        if name:
            seen_names.add(name)
        router = m.get("router") if isinstance(m.get("router"), dict) else {}
        rfile, rname = router.get("file"), router.get("name", "router")
        if not isinstance(rfile, str) or not rfile.endswith(".py"):
            errors.append(f"{p}.router.file: required python file path")
            rfile = None
        if not isinstance(rname, str) or not IDENT_RE.match(rname):
            errors.append(f"{p}.router.name: expected an identifier")
        files = m.get("files")
        if not isinstance(files, list) or not files or any(not isinstance(f, str) or not f.strip() for f in files):
            errors.append(f"{p}.files: expected a non-empty list of relative file paths")
            files = []
        for f in files:
            if f.startswith(("/", "\\")) or ".." in f.split("/") or "\\" in f:
                errors.append(f"{p}.files: {f!r} must be a relative path inside the project")
            elif f in seen_files and seen_files[f] != name:
                errors.append(f"{p}.files: {f!r} is already adopted by module {seen_files[f]!r}; a file belongs to one module")
            elif name:
                seen_files[f] = name
        if rfile and files and rfile not in files:
            errors.append(f"{p}.router.file: {rfile!r} must be listed among the module's files")
        if name and rfile and files:
            modules.append(AdoptedModule(name, rfile, rname if isinstance(rname, str) else "router", list(files)))
    notes = document.get("notes")
    if notes is not None and not isinstance(notes, str):
        errors.append("adoption.notes: expected a string")
    if errors:
        return None, sorted(set(errors))
    return AdoptionContract(project_path=str(path), modules=modules, notes=notes, source=source, document=json.loads(json.dumps(document)), schema=str(schema), extensions=extensions), []


def load_adoption_contract(path: str) -> AdoptionContract:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise AdoptionError(path, [f"cannot read the contract: {exc}"])
    except ValueError as exc:
        raise AdoptionError(path, [f"the contract is not valid JSON: {exc}"])
    contract, errors = validate_adoption(document, path)
    if contract is None:
        raise AdoptionError(path, errors)
    return contract


def _resolve_project(contract: AdoptionContract) -> Path:
    return resolve_against(contract.project_path, contract.source)


def read_adoption(project: Path) -> Optional[Dict[str, Any]]:
    path = Path(project).resolve() / ADOPTION
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") not in ADOPTION_SCHEMAS or not isinstance(data.get("modules"), dict):
        return None
    return data


def adopt(contract: AdoptionContract, *, faults: Optional[FaultInjector] = None, tool_version: str = TOOL_VERSION) -> Dict[str, Any]:
    """Validate the contract against the project and record the adoption baseline. Only ``adoption.json`` is
    written. Returns the report; raises AdoptionError when the project or the contract refuse adoption."""
    raw = _resolve_project(contract)
    if raw.is_symlink():
        raise AdoptionError(contract.source, [f"{raw} is a symlink"])
    if not raw.is_dir():
        raise AdoptionError(contract.source, [f"{raw} is not a directory"])
    project = raw.resolve()
    model = discover(str(project), audit=True)
    errors: List[str] = []
    if model.is_generated:
        errors.append("the project has a generation manifest: it is managed by the generator, use the evolution contract instead of adoption")
    if model.package is None:
        errors.append("no single application package could be determined; a project with an unknown structure is not adopted")
    existing = read_adoption(project) or {"modules": {}, "history": []}
    adopted_modules: Dict[str, Any] = {}
    for m in contract.modules:
        file_digests: Dict[str, str] = {}
        for rel in m.files:
            parts = Path(rel).parts
            if any((project.joinpath(*parts[: i + 1])).is_symlink() for i in range(len(parts))):
                errors.append(f"module {m.name}: {rel} is (or is under) a symlink; refused")
                continue
            try:
                full = safe_join(project, rel)
            except GenerationError as exc:
                errors.append(f"module {m.name}: {exc}")
                continue
            if not full.is_file():
                errors.append(f"module {m.name}: {rel} does not exist")
                continue
            if rel.endswith(".py"):
                try:
                    tree = ast.parse(full.read_bytes(), filename=rel)
                except (SyntaxError, ValueError) as exc:
                    errors.append(f"module {m.name}: {rel} does not parse ({exc}); refused")
                    continue
                if rel == m.router_file:
                    bound = module_names(tree).get(m.router_name, 0)
                    if bound != 1:
                        errors.append(f"module {m.name}: router {m.router_name!r} is bound {bound} times in {rel}; it must be bound exactly once")
                    elif not any(r["file"] == rel and r["name"] == m.router_name for r in model.routers):
                        errors.append(f"module {m.name}: {m.router_name!r} in {rel} is not an APIRouter the discovery recognises")
            file_digests[rel] = sha256_bytes(full.read_bytes())
        previous = existing["modules"].get(m.name)
        if previous is not None and (previous.get("files") != file_digests or previous.get("router") != {"file": m.router_file, "name": m.router_name}):
            if set(previous.get("files", {})) != set(file_digests):
                errors.append(f"module {m.name}: already adopted with a different file set; adoption is refreshed only for the same files")
        routes = [{"method": r["method"], "path": r["path"], "effective_paths": r["effective_paths"], "function": r["function"]} for r in model.routes if r["file"] == m.router_file]
        adopted_modules[m.name] = {"router": {"file": m.router_file, "name": m.router_name}, "files": file_digests, "routes": routes}
    for name, data in existing["modules"].items():
        if name not in adopted_modules:
            adopted_modules[name] = data  # previously adopted modules stay adopted
    extensions: Dict[str, Any] = {}
    if contract.schema == ADOPTION_SCHEMA_11:
        extensions, extension_errors = _verify_extensions(project, model, contract, existing)
        errors.extend(extension_errors)
    if errors:
        raise AdoptionError(contract.source, errors)
    all_files = sorted({f for data in adopted_modules.values() for f in data["files"]})
    generated_files = sorted({f for data in (extensions.get("generated_modules") or {}).values() for f in (data.get("files") or {})})
    protected = sorted(p for p in model.digests if p not in all_files and p not in generated_files)
    boundary = "only the files listed under modules are adopted and may be transformed; every other file is protected and is never written by the tool"
    if extensions.get("mount"):
        boundary += "; the mount file receives one import and one include_router statement per generated module, at the declared point; generated modules are recorded under generated_modules with their digests"
    manifest = {
        "schema": contract.schema,
        "tool": {"name": "fastapi-architect evolve", "version": tool_version},
        "project": {"package": model.package, "source_root": model.source_root, "layout": model.layout},
        "modules": dict(sorted(adopted_modules.items())),
        "boundary": boundary,
        "notes": contract.notes,
        **extensions,
        "history": list(existing.get("history", [])) + [{"operation": "adopt", "modules": [m.name for m in contract.modules], "contract_digest": sha256_bytes(json.dumps(contract.document, sort_keys=True).encode("utf-8"))[:16]}],
    }
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    current = project / ADOPTION
    expected = {ADOPTION: sha256_bytes(current.read_bytes()) if current.is_file() else None}
    try:
        op = write_recoverable(project, {ADOPTION: content}, expected, kind="adopt", description=f"adopt {', '.join(m.name for m in contract.modules)}", faults=faults)
    except WriteError as exc:
        raise AdoptionError(contract.source, [f"the adoption record could not be written: {exc}"]) from exc
    return {
        "project": str(project),
        "adopted": {name: {"router": data["router"], "files": sorted(data["files"]), "routes": len(data["routes"])} for name, data in manifest["modules"].items()},
        "protected_files": len(protected),
        "written": [ADOPTION],
        "operation": op.op_id,
        "uncertainties": list(model.uncertainties),
        "note": manifest["boundary"],
    }


def _verify_extensions(project: Path, model: ProjectModel, contract: AdoptionContract, existing: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The 1.1 declarations against the project: the package is the discovered one, the mount resolves, every allowed
    dependency and the Alembic environment resolve. The record keeps what was verified and the digest of the mount
    file (a later evolution refuses to edit a mount file that changed since)."""
    ext = contract.extensions
    errors: List[str] = []
    out: Dict[str, Any] = {}
    package = ext.get("package")
    if package is not None and package != model.package:
        errors.append(f"adoption.package: the contract declares {package!r} but the project's single application package is {model.package!r}")
    out["unmanaged"] = list(ext.get("unmanaged") or [])
    for g in out["unmanaged"]:
        pass
    if ext.get("mount"):
        point, mount_errors = resolve_mount(project, ext["mount"])
        errors.extend(mount_errors)
        if point is not None:
            rel = point.file
            if any(rel in (data.get("files") or {}) for data in existing.get("modules", {}).values()) or any(rel in m.files for m in contract.modules):
                errors.append(f"adoption.mount.file: {rel} is also an adopted module file; the mount file is an integration point, not a module")
            out["mount"] = {**point.to_dict(), "file_sha256": sha256_bytes((project / rel).read_bytes())}
            if not ext.get("new_modules"):
                errors.append("adoption.new_modules: required with a mount (layout, router_variable, import_style, test_app)")
    if ext.get("new_modules"):
        if not ext.get("mount"):
            errors.append("adoption.mount: required with new_modules (where the generated routers are registered)")
        test_app = ext["new_modules"]["test_app"]
        info, error = resolve_symbol(project, model.package or "", model.source_root, f"{test_app['module']}:{test_app['attribute']}")
        if error:
            errors.append(f"adoption.new_modules.test_app: {error}")
        elif test_app.get("factory") and info["kind"] not in ("function", "async_function"):
            errors.append(f"adoption.new_modules.test_app: {test_app['attribute']!r} is a {info['kind']}, not a factory function")
        out["new_modules"] = dict(ext["new_modules"])
    if ext.get("dependencies"):
        resolved = []
        for symbol in ext["dependencies"]["allowed"]:
            info, error = resolve_symbol(project, model.package or "", model.source_root, symbol)
            if error:
                errors.append(f"adoption.dependencies.allowed: {error}")
            elif info["kind"] not in ("function", "async_function", "class"):
                errors.append(f"adoption.dependencies.allowed: {symbol} is a {info['kind']}, not a dependency callable")
            else:
                resolved.append(info)
        out["dependencies"] = {"allowed": list(ext["dependencies"]["allowed"]), "resolved": resolved}
    out["operations"] = list(ext.get("operations") or [])
    if ext.get("alembic"):
        facts, alembic_errors = alembic_facts(project, model, ext["alembic"])
        errors.extend(alembic_errors)
        out["alembic"] = {**ext["alembic"], "verified": facts}
    out["generated_modules"] = dict(existing.get("generated_modules") or {})
    out["migrations"] = list(existing.get("migrations") or [])
    return out, errors


def render_proposal(proposal: Dict[str, Any]) -> str:
    lines = [f"adoption proposal for {proposal['project']} (package {proposal['package']}, layout {proposal['layout']}, generated {proposal['generated']})"]
    for c in proposal["candidates"]:
        lines.append(f"  module {c['name']}: router {c['router']['name']} in {c['router']['file']} (prefix {c['router']['prefix']!r}, mounted at {c['router']['effective_prefixes'] or '-'}), {len(c['routes'])} route(s), related files: {', '.join(c['related_files']) or '-'}")
        for r in c["routes"]:
            lines.append(f"      {r['method']:6} {', '.join(r['effective_paths']) or r['path']}  ({r['function']})")
    lines.append(f"  unassigned python files ({len(proposal['unassigned_files'])}): " + ", ".join(proposal["unassigned_files"][:12]) + (" …" if len(proposal["unassigned_files"]) > 12 else ""))
    for m in proposal.get("mount_candidates", []):
        where = f"{m['file']}::{m['function']}" if m["function"] else f"{m['file']} (module level)"
        flags = ("" if m["app_bound_here"] else "; app not bound in this scope") + (f"; dynamic registrations at lines {m['dynamic_registrations']}" if m["dynamic_registrations"] else "") + (f"; app factories: {', '.join(m['app_factories'])}" if m["app_factories"] else "")
        lines.append(f"  mount candidate: {where}: {m['after_call']}(...) x{len(m['registrations'])} ({', '.join(m['registrations']) or '-'}){flags}")
    if proposal.get("alembic"):
        al = proposal["alembic"]
        lines.append(f"  alembic: script_location {al['script_location']!r}, {al['revisions']} revision(s), heads {al['heads']}; declare alembic.metadata and alembic.session_dependency yourself (adoption 1.1)")
    if proposal.get("adoption_1_1_skeleton"):
        lines.append("  adoption 1.1 skeleton (JSON output, key adoption_1_1_skeleton): complete mount.prefix, unmanaged, dependencies.allowed, operations and alembic before adopting; nothing in it is a permission yet")
    if proposal["already_adopted"]:
        lines.append(f"  already adopted: {', '.join(proposal['already_adopted'])}")
    for u in proposal["uncertainties"]:
        lines.append(f"  ? {u}")
    lines.append(f"  note: {proposal['note']}")
    return "\n".join(lines) + "\n"


__all__ = ["ADOPTION", "ADOPTION_SCHEMA", "ADOPTION_SCHEMA_11", "ADOPTION_SCHEMAS", "AdoptionContract", "AdoptionError", "adopt", "load_adoption_contract", "propose_adoption", "read_adoption", "render_proposal", "validate_adoption", "TransformError"]
