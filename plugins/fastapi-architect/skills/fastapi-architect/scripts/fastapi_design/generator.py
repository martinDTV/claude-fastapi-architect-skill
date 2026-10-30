"""Safe, idempotent generation into an isolated target directory (v2.0).

Rules, each enforced and tested:

* the target is created when missing; an existing non-empty target is refused unless ``allow_existing`` is set,
  and then only files that do not exist yet are written;
* an existing file whose content differs from what would be generated is a **conflict**: nothing is written
  unless ``overwrite`` is set explicitly, and the conflict is reported with both digests;
* an existing file with identical content is left untouched (re-running the same spec changes nothing);
* every path is built from validated identifiers and resolved inside the target; anything that would escape it
  is refused; symlinked targets are refused; nothing is ever deleted;
* ``.fastapi-architect/generation.json`` records the spec digest, the tool version, the style and the sha256 of
  every generated file, without timestamps, so the manifest itself is reproducible.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .blueprint import Blueprint
from .spec import Spec

MANIFEST = ".fastapi-architect/generation.json"
MANIFEST_SCHEMA = "fastapi-architect/generation/1.0"


class GenerationError(RuntimeError):
    pass


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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_join(target: Path, relpath: str) -> Path:
    if relpath.startswith(("/", "\\")) or ".." in relpath.split("/") or "\\" in relpath or relpath != relpath.strip():
        raise GenerationError(f"refusing path outside the target: {relpath!r}")
    full = (target / relpath).resolve()
    if full != target and target not in full.parents:
        raise GenerationError(f"refusing path outside the target: {relpath!r}")
    return full


def build_manifest(spec: Spec, blueprint: Blueprint, tool_version: str) -> Dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "tool": {"name": "fastapi-architect design", "version": tool_version},
        "spec": {"name": spec.name, "schema": spec.schema, "digest": spec.digest},
        "style": blueprint.style,
        "package": blueprint.package,
        "files": {path: _sha(content) for path, content in sorted(blueprint.files.items())},
    }


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
    plan: List[tuple] = []
    for relpath in sorted(files):
        content = files[relpath]
        full = _safe_join(target, relpath)
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
        plan.append((relpath, full, content))
    if result.conflicts and not overwrite:
        raise GenerationError(
            f"{len(result.conflicts)} existing file(s) differ from the generated content and were not touched: "
            + ", ".join(c["path"] for c in result.conflicts[:10])
            + " (re-run with --overwrite to replace them explicitly)"
        )
    for relpath, full, content in plan:
        result.written.append(relpath)
        if dry_run:
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(full.name + ".fa-tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, full)
    return result


def verify_manifest(target_dir: str) -> Dict[str, object]:
    """Check the generated files against the manifest: ``{path: identical | modified | missing}`` plus extras."""
    target = Path(target_dir).resolve()
    manifest_path = target / MANIFEST
    if not manifest_path.is_file():
        raise GenerationError(f"no generation manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status: Dict[str, str] = {}
    for relpath, digest in sorted(manifest.get("files", {}).items()):
        full = target / relpath
        if not full.is_file():
            status[relpath] = "missing"
        else:
            status[relpath] = "identical" if _sha(full.read_text(encoding="utf-8", errors="replace")) == digest else "modified"
    return {"manifest": manifest, "status": status}
