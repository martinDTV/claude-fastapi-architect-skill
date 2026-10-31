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

from .discovery import ProjectModel, discover
from .generator import GenerationError, safe_join
from .journal import FaultInjector, sha256_bytes, write_recoverable
from .spec import IDENT_RE
from .transforms import TransformError, module_names

ADOPTION = ".fastapi-architect/adoption.json"
ADOPTION_SCHEMA = "fastapi-architect/adoption/1.0"


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

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": ADOPTION_SCHEMA, "project_path": self.project_path, "modules": [m.to_dict() for m in self.modules], "notes": self.notes}


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
    return {
        "project": model.root,
        "package": model.package,
        "layout": model.layout,
        "generated": model.is_generated,
        "already_adopted": sorted((model.adoption or {}).get("modules", {}).keys()) if getattr(model, "adoption", None) else [],
        "candidates": candidates,
        "unassigned_files": unassigned,
        "uncertainties": list(model.uncertainties),
        "note": "nothing is adopted by this proposal: write an adoption contract naming the modules and the exact files to adopt; every other file stays protected",
    }


def validate_adoption(document: Any, source: Optional[str] = None) -> Tuple[Optional[AdoptionContract], List[str]]:
    errors: List[str] = []
    if not isinstance(document, dict):
        return None, ["top level: expected an object"]
    for key in document:
        if key not in ("schema", "project", "modules", "notes") and not str(key).startswith("x-"):
            errors.append(f"adoption.{key}: unknown property")
    if document.get("schema") != ADOPTION_SCHEMA:
        errors.append(f"adoption.schema: expected {ADOPTION_SCHEMA!r}, got {document.get('schema')!r}")
    project = document.get("project") if isinstance(document.get("project"), dict) else {}
    path = project.get("path")
    if not isinstance(path, str) or not path.strip():
        errors.append("adoption.project.path: required non-empty string")
    modules_raw = document.get("modules")
    modules: List[AdoptedModule] = []
    if not isinstance(modules_raw, list) or not modules_raw:
        errors.append("adoption.modules: expected a non-empty list")
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
    return AdoptionContract(project_path=str(path), modules=modules, notes=notes, source=source, document=json.loads(json.dumps(document))), []


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
    raw = Path(contract.project_path)
    if not raw.is_absolute() and contract.source:
        raw = Path(contract.source).resolve().parent / raw
    return raw


def read_adoption(project: Path) -> Optional[Dict[str, Any]]:
    path = Path(project).resolve() / ADOPTION
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ADOPTION_SCHEMA or not isinstance(data.get("modules"), dict):
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
    if errors:
        raise AdoptionError(contract.source, errors)
    all_files = sorted({f for data in adopted_modules.values() for f in data["files"]})
    protected = sorted(p for p in model.digests if p not in all_files)
    manifest = {
        "schema": ADOPTION_SCHEMA,
        "tool": {"name": "fastapi-architect evolve", "version": tool_version},
        "project": {"package": model.package, "source_root": model.source_root, "layout": model.layout},
        "modules": dict(sorted(adopted_modules.items())),
        "boundary": "only the files listed under modules are adopted and may be transformed; every other file is protected and is never written by the tool",
        "notes": contract.notes,
        "history": list(existing.get("history", [])) + [{"operation": "adopt", "modules": [m.name for m in contract.modules], "contract_digest": sha256_bytes(json.dumps(contract.document, sort_keys=True).encode("utf-8"))[:16]}],
    }
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    current = project / ADOPTION
    expected = {ADOPTION: sha256_bytes(current.read_bytes()) if current.is_file() else None}
    op = write_recoverable(project, {ADOPTION: content}, expected, kind="adopt", description=f"adopt {', '.join(m.name for m in contract.modules)}", faults=faults)
    return {
        "project": str(project),
        "adopted": {name: {"router": data["router"], "files": sorted(data["files"]), "routes": len(data["routes"])} for name, data in manifest["modules"].items()},
        "protected_files": len(protected),
        "written": [ADOPTION],
        "operation": op.op_id,
        "uncertainties": list(model.uncertainties),
        "note": manifest["boundary"],
    }


def render_proposal(proposal: Dict[str, Any]) -> str:
    lines = [f"adoption proposal for {proposal['project']} (package {proposal['package']}, layout {proposal['layout']}, generated {proposal['generated']})"]
    for c in proposal["candidates"]:
        lines.append(f"  module {c['name']}: router {c['router']['name']} in {c['router']['file']} (prefix {c['router']['prefix']!r}, mounted at {c['router']['effective_prefixes'] or '-'}), {len(c['routes'])} route(s), related files: {', '.join(c['related_files']) or '-'}")
        for r in c["routes"]:
            lines.append(f"      {r['method']:6} {', '.join(r['effective_paths']) or r['path']}  ({r['function']})")
    lines.append(f"  unassigned python files ({len(proposal['unassigned_files'])}): " + ", ".join(proposal["unassigned_files"][:12]) + (" …" if len(proposal["unassigned_files"]) > 12 else ""))
    if proposal["already_adopted"]:
        lines.append(f"  already adopted: {', '.join(proposal['already_adopted'])}")
    for u in proposal["uncertainties"]:
        lines.append(f"  ? {u}")
    lines.append(f"  note: {proposal['note']}")
    return "\n".join(lines) + "\n"


__all__ = ["ADOPTION", "ADOPTION_SCHEMA", "AdoptionContract", "AdoptionError", "adopt", "load_adoption_contract", "propose_adoption", "read_adoption", "render_proposal", "validate_adoption", "TransformError"]
