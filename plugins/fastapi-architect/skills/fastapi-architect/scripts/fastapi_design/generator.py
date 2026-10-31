"""Safe, idempotent generation into an isolated target directory (v2.0).

Rules, each enforced and tested:

* the target is created when missing; an existing non-empty target is refused unless ``allow_existing`` is set,
  and then only files that do not exist yet are written;
* an existing file whose content differs from what would be generated is a **conflict**: nothing is written
  unless ``overwrite`` is set explicitly, and the conflict is reported with both digests;
* an existing file with identical content is left untouched (re-running the same spec changes nothing);
* every path is built from validated identifiers and resolved inside the target; anything that would escape it
  is refused; symlinked targets are refused; nothing is ever deleted;
* ``.fastapi-architect/generation.json`` records the spec digest, the tool version, the style, the sha256 of
  every generated file, the contract document itself, the migration chain and the operation history, without
  timestamps, so the manifest itself is reproducible (schema 1.1; 1.0 manifests are still read).

``write_files`` is the two-phase writer shared with the evolution engine: every precondition (expected digest or
absence of each file) is re-checked immediately before writing, temporary files are written first, and only then
renamed into place; a failure before the renames leaves the project untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .blueprint import Blueprint
from .spec import Spec

MANIFEST = ".fastapi-architect/generation.json"
MANIFEST_SCHEMA = "fastapi-architect/generation/1.1"
MANIFEST_SCHEMAS = ("fastapi-architect/generation/1.0", "fastapi-architect/generation/1.1")


class GenerationError(RuntimeError):
    pass


class WriteError(GenerationError):
    """A precondition failed or the filesystem refused a write; nothing was renamed into place."""


@dataclass
class GenerationResult:
    target: str
    written: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    manifest: Dict[str, object] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.written)

    def to_dict(self) -> Dict[str, object]:
        return {
            "target": self.target,
            "dry_run": self.dry_run,
            "written": list(self.written),
            "unchanged": list(self.unchanged),
            "conflicts": list(self.conflicts),
            "manifest": dict(self.manifest),
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_sha = sha256_text


def safe_join(target: Path, relpath: str) -> Path:
    """Resolve ``relpath`` inside ``target``; refuse absolute paths, ``..``, backslashes, surrounding spaces and
    anything that resolves (through symlinks) outside the target."""
    if not relpath or relpath.startswith(("/", "\\")) or ".." in relpath.split("/") or "\\" in relpath or relpath != relpath.strip():
        raise GenerationError(f"refusing path outside the target: {relpath!r}")
    full = (target / relpath).resolve()
    if full != target and target not in full.parents:
        raise GenerationError(f"refusing path outside the target: {relpath!r}")
    return full


_safe_join = safe_join


def _no_symlink_on_path(target: Path, relpath: str) -> None:
    """Refuse to write through a symlinked file or directory inside the target."""
    current = target
    for part in relpath.split("/"):
        current = current / part
        if current.is_symlink():
            raise GenerationError(f"refusing to write through a symlink: {relpath!r} ({current.relative_to(target)})")


def build_manifest(spec: Spec, blueprint: Blueprint, tool_version: str, *, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Manifest 1.1: file digests plus the contract document, the migration chain and the operation history."""
    return {
        "schema": MANIFEST_SCHEMA,
        "tool": {"name": "fastapi-architect design", "version": tool_version},
        "spec": {"name": spec.name, "schema": spec.schema, "digest": spec.digest},
        "spec_document": spec.document,
        "style": blueprint.style,
        "package": blueprint.package,
        "files": {path: _sha(content) for path, content in sorted(blueprint.files.items())},
        "migrations": list(blueprint.migrations),
        "history": list(history) if history is not None else [{"operation": "generate", "spec_digest": spec.digest, "modules": [m.name for m in spec.modules]}],
    }


def read_manifest(target_dir: str) -> Dict[str, Any]:
    path = Path(target_dir).resolve() / MANIFEST
    if not path.is_file():
        raise GenerationError(f"no generation manifest at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") not in MANIFEST_SCHEMAS:
        raise GenerationError(f"unknown manifest schema in {path}")
    return manifest


def write_files(target: Path, files: Dict[str, str], expected: Dict[str, Optional[str]]) -> List[str]:
    """Two-phase atomic write. ``expected[rel]`` is the sha256 the file must currently have, or None when it must
    not exist. Every precondition is checked first (concurrent changes abort before any write), then every file is
    written to a temporary sibling, then all temporaries are renamed into place. Returns the written paths."""
    target = target.resolve()
    plan: List[tuple] = []
    for rel in sorted(files):
        full = safe_join(target, rel)
        _no_symlink_on_path(target, rel)
        exp = expected.get(rel)
        if full.exists():
            if full.is_dir():
                raise WriteError(f"a directory sits where a file would be written: {rel}")
            current = _sha(full.read_text(encoding="utf-8", errors="replace"))
            if exp is None:
                raise WriteError(f"concurrent change: {rel} appeared after the plan was computed")
            if current != exp:
                raise WriteError(f"concurrent change: {rel} changed after the plan was computed (expected {exp[:12]}, found {current[:12]})")
        elif exp is not None:
            raise WriteError(f"concurrent change: {rel} disappeared after the plan was computed")
        plan.append((rel, full, files[rel]))
    temps: List[Path] = []
    try:
        for rel, full, content in plan:
            full.parent.mkdir(parents=True, exist_ok=True)
            tmp = full.with_name(full.name + ".fa-tmp")
            tmp.write_text(content, encoding="utf-8")
            temps.append(tmp)
    except OSError as exc:
        for tmp in temps:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise WriteError(f"write failed before anything was replaced: {exc}") from exc
    written: List[str] = []
    for (rel, full, _content), tmp in zip(plan, temps):
        os.replace(tmp, full)
        written.append(rel)
    return written


def generate(spec: Spec, blueprint: Blueprint, target_dir: str, *, tool_version: str, allow_existing: bool = False, overwrite: bool = False, dry_run: bool = False) -> GenerationResult:
    raw = Path(target_dir)
    if raw.is_symlink():  # checked before resolving: the resolved path would hide the link
        raise GenerationError(f"the target is a symlink: {raw}")
    target = raw.resolve()
    if target.exists() and not target.is_dir():
        raise GenerationError(f"the target exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()) and not allow_existing and not overwrite:
        existing_manifest = target / MANIFEST
        if not existing_manifest.is_file():
            raise GenerationError(f"the target is not empty: {target} (use --allow-existing to add missing files only, or --overwrite to replace conflicting files)")
    manifest = build_manifest(spec, blueprint, tool_version)
    files = dict(blueprint.files)
    files[MANIFEST] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    result = GenerationResult(target=str(target), manifest=manifest, dry_run=dry_run)
    to_write: Dict[str, str] = {}
    expected: Dict[str, Optional[str]] = {}
    for relpath in sorted(files):
        content = files[relpath]
        full = safe_join(target, relpath)
        if full.exists():
            if full.is_dir():
                raise GenerationError(f"a directory sits where a file would be written: {relpath}")
            current = full.read_text(encoding="utf-8", errors="replace")
            if current == content:
                result.unchanged.append(relpath)
                continue
            if not overwrite:
                result.conflicts.append({"path": relpath, "existing_sha256": _sha(current), "generated_sha256": _sha(content)})
                continue
            expected[relpath] = _sha(current)
        else:
            expected[relpath] = None
        to_write[relpath] = content
    if result.conflicts and not overwrite:
        raise GenerationError(
            f"{len(result.conflicts)} existing file(s) differ from the generated content and were not touched: "
            + ", ".join(c["path"] for c in result.conflicts[:10])
            + " (re-run with --overwrite to replace them explicitly)"
        )
    result.written = sorted(to_write)
    if dry_run:
        return result
    if to_write:
        write_files(target, to_write, expected)
    return result


def verify_manifest(target_dir: str) -> Dict[str, object]:
    """Check the generated files against the manifest: ``{path: identical | modified | missing}``."""
    target = Path(target_dir).resolve()
    manifest = read_manifest(str(target))
    status: Dict[str, str] = {}
    for relpath, digest in sorted(manifest.get("files", {}).items()):
        full = target / relpath
        if not full.is_file():
            status[relpath] = "missing"
        else:
            status[relpath] = "identical" if _sha(full.read_text(encoding="utf-8", errors="replace")) == digest else "modified"
    return {"manifest": manifest, "status": status}
