"""Recoverable multi-file writes (v2.0, increment 4): prepare, publish, recover.

The filesystem gives us one primitive: ``os.replace`` of a file is atomic on POSIX when source and target live on
the same filesystem. It gives us nothing across files. A plan that touches eleven files can therefore be
interrupted after any number of them. This module does not pretend otherwise; it makes the interruption
**recoverable**:

1. ``prepare``: every precondition is checked (paths inside the project, no symlink on the way, the current
   digest of every file equals what the plan expected, or the file is absent when the plan creates it); the new
   content of every file and a backup of every existing file are copied under
   ``.fastapi-architect/ops/<operation id>/``; a journal (``journal.json``) records the operation: id, kind,
   description, the files with their expected and new digests, and the state ``prepared``. The operation id is
   the sha256 of the plan (paths, expected digests, new digests, kind, description): the same plan always has the
   same id, which is how a retry of a completed operation is recognised.
2. ``publish``: the journal moves to ``publishing``; preconditions are re-checked immediately before the first
   write (a concurrent change aborts with nothing published); each file is written to a temporary sibling and
   renamed into place; the journal records every published path as it lands; then ``published``; then the staging
   directory is removed and the journal is deleted (``completed``).
3. ``recover``: a journal left in ``publishing`` (or ``published``) means an interruption. Every file is classified
   by its current digest: ``published`` (equals the new digest), ``pending`` (equals the expected digest, or absent
   when it was to be created), ``conflict`` (anything else: somebody changed it after the interruption). The
   strategy is ``rollback`` (restore the backups of the published files, remove the files this operation created,
   leave pending files alone) or ``forward`` (publish the pending files from the staging copies). Files in
   conflict are never touched; they are reported and the journal stays until the operator resolves them. Recovery
   records its own progress in the journal, so an interrupted recovery can be resumed, and it is idempotent:
   running it twice changes nothing the second time.

Limits, stated plainly: there is no global atomicity; a crash between two renames leaves a mixed tree until
``recover`` runs; a crash during ``prepare`` leaves only staging files under ``.fastapi-architect/ops`` and nothing
else; the staging directory must be on the same filesystem as the project for the renames to be atomic; nothing
here protects against another writer that ignores the journal. Fault injection points (``FaultInjector``) exist so
the tests can interrupt the protocol before, during and after publication and during recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .generator import GenerationError, WriteError, safe_join

OPS_DIR = ".fastapi-architect/ops"
JOURNAL = "journal.json"
JOURNAL_SCHEMA = "fastapi-architect/operation/1.0"
STATES = ("prepared", "publishing", "published", "recovering_rollback", "recovering_forward", "needs_attention")


class InjectedFailure(OSError):
    """Raised by a FaultInjector at the requested point; behaves like an I/O error."""


@dataclass
class FaultInjector:
    """``points``: ``before_publish``, ``after_publish:<n>`` (after the n-th file landed), ``after_publish_all``,
    ``before_cleanup``, ``recover_after:<n>`` (after the n-th file was restored or published during recovery)."""

    points: List[str] = field(default_factory=list)
    hits: List[str] = field(default_factory=list)

    def check(self, point: str) -> None:
        if point in self.points:
            self.hits.append(point)
            raise InjectedFailure(f"injected failure at {point}")


@dataclass
class FileEntry:
    path: str
    expected: Optional[str]  # sha256 the file must have before the operation; None = must not exist
    new: str  # sha256 after the operation

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {"path": self.path, "expected": self.expected, "new": self.new}


@dataclass
class Operation:
    op_id: str
    kind: str
    description: str
    files: List[FileEntry]
    state: str = "prepared"
    published: List[str] = field(default_factory=list)  # paths renamed into place, in order
    recovered: List[str] = field(default_factory=list)  # paths handled by recovery, in order
    conflicts: Dict[str, str] = field(default_factory=dict)  # path -> detail (recovery)

    @property
    def directory(self) -> str:
        return f"{OPS_DIR}/{self.op_id}"

    def to_dict(self) -> Dict[str, object]:
        return {"schema": JOURNAL_SCHEMA, "op_id": self.op_id, "kind": self.kind, "description": self.description, "state": self.state, "files": [f.to_dict() for f in self.files], "published": list(self.published), "recovered": list(self.recovered), "conflicts": dict(self.conflicts)}

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Operation":
        files = [FileEntry(str(f["path"]), f.get("expected"), str(f["new"])) for f in data.get("files", [])]  # type: ignore[index,union-attr]
        return cls(op_id=str(data["op_id"]), kind=str(data.get("kind", "")), description=str(data.get("description", "")), files=files, state=str(data.get("state", "prepared")), published=list(data.get("published", [])), recovered=list(data.get("recovered", [])), conflicts=dict(data.get("conflicts", {})))  # type: ignore[arg-type]


@dataclass
class RecoveryReport:
    op_id: str
    strategy: str
    restored: List[str] = field(default_factory=list)  # rollback: files restored or removed
    published: List[str] = field(default_factory=list)  # forward: files published
    already_done: List[str] = field(default_factory=list)
    conflicts: Dict[str, str] = field(default_factory=dict)
    state: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"op_id": self.op_id, "strategy": self.strategy, "restored": list(self.restored), "published": list(self.published), "already_done": list(self.already_done), "conflicts": dict(self.conflicts), "state": self.state}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def operation_id(files: Dict[str, str], expected: Dict[str, Optional[str]], kind: str, description: str) -> str:
    payload = {"kind": kind, "description": description, "files": [{"path": p, "expected": expected.get(p), "new": sha256_bytes(files[p].encode("utf-8"))} for p in sorted(files)]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".fa-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save_journal(target: Path, op: Operation) -> None:
    _write_atomic(target / op.directory / JOURNAL, (json.dumps(op.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _no_symlink_on_path(target: Path, relpath: str) -> None:
    current = target
    for part in relpath.split("/"):
        current = current / part
        if current.is_symlink():
            raise GenerationError(f"refusing to write through a symlink: {relpath!r}")


def _check_preconditions(target: Path, files: List[FileEntry]) -> None:
    for entry in files:
        full = safe_join(target, entry.path)
        _no_symlink_on_path(target, entry.path)
        current = _digest(full)
        if full.exists() and full.is_dir():
            raise WriteError(f"a directory sits where a file would be written: {entry.path}")
        if entry.expected is None and current is not None:
            raise WriteError(f"concurrent change: {entry.path} appeared after the plan was computed")
        if entry.expected is not None and current is None:
            raise WriteError(f"concurrent change: {entry.path} disappeared after the plan was computed")
        if entry.expected is not None and current != entry.expected:
            raise WriteError(f"concurrent change: {entry.path} changed after the plan was computed (expected {entry.expected[:12]}, found {str(current)[:12]})")


def prepare(target: Path, files: Dict[str, str], expected: Dict[str, Optional[str]], *, kind: str, description: str) -> Operation:
    """Check every precondition, stage the new content and the backups, write the journal (state ``prepared``).
    Nothing in the project is modified. Raises WriteError/GenerationError and leaves no staging behind on failure."""
    target = target.resolve()
    if target.is_symlink():
        raise GenerationError(f"the project is a symlink: {target}")
    op_id = operation_id(files, expected, kind, description)
    entries = [FileEntry(p, expected.get(p), sha256_bytes(files[p].encode("utf-8"))) for p in sorted(files)]
    _check_preconditions(target, entries)
    op_dir = target / OPS_DIR / op_id
    if op_dir.exists():
        existing = load_operation(target, op_id)
        if existing is not None and existing.state != "prepared":
            raise WriteError(f"operation {op_id} is already {existing.state}; recover it before preparing it again")
        shutil.rmtree(op_dir, ignore_errors=True)
    op = Operation(op_id=op_id, kind=kind, description=description, files=entries)
    try:
        for entry in entries:
            (op_dir / "new" / entry.path).parent.mkdir(parents=True, exist_ok=True)
            (op_dir / "new" / entry.path).write_bytes(files[entry.path].encode("utf-8"))
            if entry.expected is not None:
                (op_dir / "backup" / entry.path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target / entry.path, op_dir / "backup" / entry.path)
        _save_journal(target, op)
    except OSError as exc:
        shutil.rmtree(op_dir, ignore_errors=True)
        raise WriteError(f"could not stage the operation: {exc}") from exc
    return op


def publish(target: Path, op: Operation, *, faults: Optional[FaultInjector] = None) -> List[str]:
    """Rename every staged file into place, journaling each landing; then clean up. Raises on interruption; the
    journal then describes exactly what landed (``recover`` finishes or undoes the rest)."""
    target = target.resolve()
    faults = faults or FaultInjector()
    op_dir = target / op.directory
    if op.state == "prepared":
        _check_preconditions(target, op.files)  # the world may have moved since prepare
    faults.check("before_publish")
    op.state = "publishing"
    _save_journal(target, op)
    for index, entry in enumerate(op.files, start=1):
        if entry.path in op.published:
            continue
        full = safe_join(target, entry.path)
        _no_symlink_on_path(target, entry.path)
        _write_atomic(full, (op_dir / "new" / entry.path).read_bytes())
        op.published.append(entry.path)
        _save_journal(target, op)
        faults.check(f"after_publish:{index}")
    faults.check("after_publish_all")
    op.state = "published"
    _save_journal(target, op)
    faults.check("before_cleanup")
    complete(target, op)
    return [f.path for f in op.files]


def complete(target: Path, op: Operation) -> None:
    """Remove the staging directory and the journal of a fully published operation."""
    shutil.rmtree(target / op.directory, ignore_errors=True)
    ops = target / OPS_DIR
    if ops.is_dir() and not any(ops.iterdir()):
        ops.rmdir()


def load_operation(target: Path, op_id: str) -> Optional[Operation]:
    path = Path(target).resolve() / OPS_DIR / op_id / JOURNAL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("schema") != JOURNAL_SCHEMA:
        return None
    return Operation.from_dict(data)


def pending_operations(target: Path) -> List[Operation]:
    """Operations whose journal is still on disk: interrupted or awaiting attention."""
    ops = Path(target).resolve() / OPS_DIR
    if not ops.is_dir():
        return []
    out: List[Operation] = []
    for child in sorted(ops.iterdir()):
        op = load_operation(target, child.name)
        if op is not None:
            out.append(op)
    return out


def classify(target: Path, op: Operation) -> Dict[str, str]:
    """``path -> published | pending | conflict`` by current content."""
    target = Path(target).resolve()
    out: Dict[str, str] = {}
    for entry in op.files:
        current = _digest(target / entry.path)
        if current == entry.new:
            out[entry.path] = "published"
        elif current == entry.expected:
            out[entry.path] = "pending"
        else:
            out[entry.path] = "conflict"
    return out


def recover(target: Path, op: Operation, *, strategy: str = "rollback", faults: Optional[FaultInjector] = None) -> RecoveryReport:
    """Finish (``forward``) or undo (``rollback``) an interrupted operation. Files in conflict are never touched.
    Progress is journaled; calling it again continues or confirms (idempotent)."""
    if strategy not in ("rollback", "forward"):
        raise ValueError("strategy must be 'rollback' or 'forward'")
    target = Path(target).resolve()
    faults = faults or FaultInjector()
    op_dir = target / op.directory
    report = RecoveryReport(op_id=op.op_id, strategy=strategy)
    if op.state == "prepared":  # nothing was published: discarding the staging is the whole recovery
        complete(target, op)
        report.state = "discarded"
        return report
    op.state = f"recovering_{strategy}"
    _save_journal(target, op)
    status = classify(target, op)
    handled = 0
    for entry in op.files:
        kind = status[entry.path]
        full = target / entry.path
        if kind == "conflict":
            op.conflicts[entry.path] = f"content is neither the expected nor the new version (found {str(_digest(full))[:12]}); left untouched"
            report.conflicts[entry.path] = op.conflicts[entry.path]
            continue
        if strategy == "rollback":
            if kind == "published":
                if entry.expected is None:
                    full.unlink()  # created by this operation and still exactly what we wrote
                else:
                    _write_atomic(full, (op_dir / "backup" / entry.path).read_bytes())
                report.restored.append(entry.path)
                handled += 1
            else:
                report.already_done.append(entry.path)
        else:
            if kind == "pending":
                _write_atomic(full, (op_dir / "new" / entry.path).read_bytes())
                report.published.append(entry.path)
                handled += 1
            else:
                report.already_done.append(entry.path)
        if entry.path not in op.recovered:
            op.recovered.append(entry.path)
        _save_journal(target, op)
        if handled:
            faults.check(f"recover_after:{handled}")
    if report.conflicts:
        op.state = "needs_attention"
        _save_journal(target, op)
        report.state = "needs_attention"
        return report
    complete(target, op)
    report.state = "rolled_back" if strategy == "rollback" else "completed"
    return report


def write_recoverable(target: Path, files: Dict[str, str], expected: Dict[str, Optional[str]], *, kind: str, description: str, faults: Optional[FaultInjector] = None) -> Operation:
    """prepare + publish; on failure the journal stays for ``recover``. Returns the operation (state as left)."""
    op = prepare(target, files, expected, kind=kind, description=description)
    publish(target, op, faults=faults)
    return op
