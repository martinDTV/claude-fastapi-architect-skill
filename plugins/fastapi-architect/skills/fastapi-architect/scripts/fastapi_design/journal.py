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
here protects against another writer that ignores the journal.

Durability (v2.1, increment 0): every write of the protocol (staged content, backups, each journal state, each
landed file, each restored file, the cleanup) goes through ``fastapi_common.fs.atomic_write(durable=True)``: the file is
fsync'ed before the rename and the directory entry after it, and the directories that hold a new operation are
synced too. Once a call returns, its effects survive a crash or a power loss on a filesystem that honours ``fsync``.
Not guaranteed: the drive's own cache on macOS (``F_FULLFSYNC`` is not issued), directory entries on Windows, and
filesystems that refuse to sync a directory (the file is synced, the entry is not, and the writer reports it rather
than failing). ``durable=False`` exists for throwaway verification copies only; a failed fsync of a file is an error.

Fault injection points (``FaultInjector``) exist so the tests can interrupt the protocol before, during and after
publication and during recovery.

v2.2 (increment 0), additive: the journal is ``fastapi-architect/operation/1.1`` (1.0 journals are still read and
recovered); an operation may carry ``steps`` (what produced each file: ``regenerate``, ``merge``, ``transform``,
``manifest`` …, with their evidence) and a ``parent_op``; ``write_recoverable`` and ``recover`` take the project lock
of ``locking.py`` (fail-fast by default, bounded wait on request) so one writer at a time touches a project.

v2.2 (increment 5), hardening, same schema: ``prepare`` refuses while any other operation of the project is pending
or unreadable (an ambiguous tree is resolved before it is written again); every file's digest is re-checked
immediately before its own rename (a file changed during the publication is refused and recorded, never
overwritten); ``guards`` are files the plan read but does not write, whose digests must hold at prepare and publish;
the directories a publication creates are recorded (``created_dirs``) and removed again by a rollback when empty;
the staged copies and backups are verified against the journal before recovery uses them; recovery resolves every
path through ``safe_join`` and refuses symlinks introduced after the plan; an interrupted recovery resumes only with
the strategy it started with; ``completed`` is written before the cleanup so an interrupted cleanup is finished, not
mistaken for an interrupted publication; a journal that does not validate (duplicate or unsafe paths, malformed
digests, unknown state, a ``published`` entry that names no file) is reported as unreadable instead of being
trusted; leftover temporaries of the writer (``*.fa-tmp``) are listed for inspection and never deleted here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi_common.fs import TEMP_SUFFIX, atomic_write, fsync_directory, sha256_bytes, sha256_path, symlink_on_path

from .generator import GenerationError, WriteError, safe_join
from .locking import LockHeld, project_lock  # noqa: F401  (LockHeld re-exported for callers of the writer)

OPS_DIR = ".fastapi-architect/ops"
JOURNAL = "journal.json"
JOURNAL_SCHEMA = "fastapi-architect/operation/1.1"  # 1.1 (v2.2): steps and parent_op, additive
JOURNAL_SCHEMAS = ("fastapi-architect/operation/1.0", JOURNAL_SCHEMA)
STATES = ("prepared", "publishing", "published", "recovering_rollback", "recovering_forward", "needs_attention", "completed")
RECOVERING = {"recovering_rollback": "rollback", "recovering_forward": "forward"}
NOT_WRITTEN = "changed before it was written"  # the detail prefix of a file the publication refused (never renamed)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}


class InjectedFailure(OSError):
    """Raised by a FaultInjector at the requested point; behaves like an I/O error."""


@dataclass
class FaultInjector:
    """``points``: ``before_prepare``, ``after_stage:<n>`` (after the n-th file was staged), ``before_publish``,
    ``after_journal`` (the journal says ``publishing``, nothing renamed yet), ``after_publish:<n>`` (after the n-th
    file landed), ``after_publish_all``, ``before_cleanup``, ``during_cleanup`` (the journal says ``completed``,
    the staging is still there), ``before_recover``, ``during_classify`` (while recovery re-reads the files),
    ``recover_after:<n>`` (after the n-th file was restored or published during recovery)."""

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
    conflicts: Dict[str, str] = field(default_factory=dict)  # path -> detail (recovery, or a file refused during publication)
    steps: List[Dict[str, object]] = field(default_factory=list)  # 1.1: {kind, paths, evidence} per producing step
    parent_op: Optional[str] = None  # 1.1: the operation this one continues (a chain of phases)
    guards: Dict[str, Optional[str]] = field(default_factory=dict)  # increment 5: files read by the plan, not written; digest that must hold
    created_dirs: List[str] = field(default_factory=list)  # increment 5: directories the publication created (deepest last)
    recovery_strategy: Optional[str] = None  # increment 5: the strategy a recovery started with; a later run must keep it

    @property
    def directory(self) -> str:
        return f"{OPS_DIR}/{self.op_id}"

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {"schema": JOURNAL_SCHEMA, "op_id": self.op_id, "kind": self.kind, "description": self.description, "state": self.state, "files": [f.to_dict() for f in self.files], "published": list(self.published), "recovered": list(self.recovered), "conflicts": dict(self.conflicts), "steps": [dict(s) for s in self.steps], "parent_op": self.parent_op}
        if self.guards:
            out["guards"] = dict(self.guards)
        if self.created_dirs:
            out["created_dirs"] = list(self.created_dirs)
        if self.recovery_strategy:
            out["recovery_strategy"] = self.recovery_strategy
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Operation":
        files = [FileEntry(str(f["path"]), f.get("expected"), str(f["new"])) for f in data.get("files", [])]  # type: ignore[index,union-attr]
        guards = data.get("guards") if isinstance(data.get("guards"), dict) else {}
        return cls(op_id=str(data["op_id"]), kind=str(data.get("kind", "")), description=str(data.get("description", "")), files=files, state=str(data.get("state", "prepared")), published=list(data.get("published", [])), recovered=list(data.get("recovered", [])), conflicts=dict(data.get("conflicts", {})), steps=[dict(s) for s in data.get("steps", []) if isinstance(s, dict)], parent_op=(str(data["parent_op"]) if data.get("parent_op") else None), guards={str(k): (None if v is None else str(v)) for k, v in guards.items()}, created_dirs=[str(d) for d in data.get("created_dirs", []) or []], recovery_strategy=(str(data["recovery_strategy"]) if data.get("recovery_strategy") in ("rollback", "forward") else None))  # type: ignore[arg-type,index,union-attr]


@dataclass
class RecoveryReport:
    op_id: str
    strategy: str
    restored: List[str] = field(default_factory=list)  # rollback: files restored or removed
    published: List[str] = field(default_factory=list)  # forward: files published
    already_done: List[str] = field(default_factory=list)
    conflicts: Dict[str, str] = field(default_factory=dict)
    state: str = ""
    removed_dirs: List[str] = field(default_factory=list)  # rollback: directories this operation had created, removed when empty

    def to_dict(self) -> Dict[str, object]:
        return {"op_id": self.op_id, "strategy": self.strategy, "restored": list(self.restored), "published": list(self.published), "already_done": list(self.already_done), "conflicts": dict(self.conflicts), "state": self.state, "removed_dirs": list(self.removed_dirs)}


def _digest(path: Path) -> Optional[str]:
    return sha256_path(path)


def operation_id(files: Dict[str, str], expected: Dict[str, Optional[str]], kind: str, description: str) -> str:
    payload = {"kind": kind, "description": description, "files": [{"path": p, "expected": expected.get(p), "new": sha256_bytes(files[p].encode("utf-8"))} for p in sorted(files)]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _write_atomic(path: Path, data: bytes, *, durable: bool) -> None:
    """Temporary sibling + rename; with ``durable`` the file and its directory entry are fsync'ed (``fastapi_common.fs``)."""
    atomic_write(path, data, durable=durable)


def _sync_directories(target: Path, op_dir: Path, durable: bool) -> None:
    """Make the creation of the operation directory itself durable: its parents up to the project root."""
    if not durable:
        return
    for directory in (op_dir.parent, op_dir.parent.parent, target):
        if directory.is_dir():
            fsync_directory(directory)


def _save_journal(target: Path, op: Operation, *, durable: bool) -> None:
    _write_atomic(target / op.directory / JOURNAL, (json.dumps(op.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"), durable=durable)


def _no_symlink_on_path(target: Path, relpath: str) -> None:
    if symlink_on_path(target, relpath) is not None:
        raise GenerationError(f"refusing to write through a symlink: {relpath!r}")


def _safe_relpath(relpath: object) -> Optional[str]:
    """The reason a journal path is unsafe, or None."""
    if not isinstance(relpath, str) or not relpath:
        return "empty or non-string path"
    if relpath.startswith(("/", "\\")) or ".." in relpath.split("/") or "\\" in relpath or relpath != relpath.strip() or "\x00" in relpath:
        return f"unsafe path {relpath!r}"
    if relpath.startswith(OPS_DIR + "/") or relpath == OPS_DIR:
        return f"path inside the operations directory {relpath!r}"
    return None


def validate_journal(data: object, op_id: Optional[str] = None) -> Optional[str]:
    """Why a journal document cannot be trusted, or None when it is well-formed: schema, identity, unique and safe
    paths, sha256 digests, a known state, ``published``/``recovered`` naming only files of the operation, steps and
    guards of the right shape. A journal that fails here is reported as unreadable, never acted upon."""
    if not isinstance(data, dict):
        return "journal.json is not an object"
    if data.get("schema") not in JOURNAL_SCHEMAS:
        return f"journal.json has schema {data.get('schema')!r}"
    if not isinstance(data.get("op_id"), str) or not re.match(r"^[0-9a-f]{16}$", data["op_id"]):
        return "journal.json has no valid op_id"
    if op_id is not None and data["op_id"] != op_id:
        return f"journal.json names operation {data['op_id']!r} but sits in directory {op_id!r}"
    if data.get("state") not in STATES:
        return f"journal.json has unknown state {data.get('state')!r}"
    files = data.get("files")
    if not isinstance(files, list):
        return "journal.json has no file list"
    seen: set = set()
    for entry in files:
        if not isinstance(entry, dict):
            return "journal.json has a file entry that is not an object"
        reason = _safe_relpath(entry.get("path"))
        if reason:
            return f"journal.json: {reason}"
        if entry["path"] in seen:
            return f"journal.json lists {entry['path']!r} twice"
        seen.add(entry["path"])
        if entry.get("expected") is not None and not (isinstance(entry.get("expected"), str) and _DIGEST_RE.match(entry["expected"])):
            return f"journal.json: {entry['path']!r} has a malformed expected digest"
        if not isinstance(entry.get("new"), str) or not _DIGEST_RE.match(entry["new"]):
            return f"journal.json: {entry['path']!r} has a malformed new digest"
    for key in ("published", "recovered"):
        names = data.get(key, [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            return f"journal.json: {key} is not a list of paths"
        if len(set(names)) != len(names):
            return f"journal.json: {key} repeats a path"
        unknown = [n for n in names if n not in seen]
        if unknown:
            return f"journal.json: {key} names {unknown[0]!r}, which is not a file of the operation"
    if not isinstance(data.get("conflicts", {}), dict):
        return "journal.json: conflicts is not an object"
    if not isinstance(data.get("steps", []), list) or any(not isinstance(s, dict) for s in data.get("steps", [])):
        return "journal.json: steps is not a list of objects"
    if data.get("parent_op") is not None and not isinstance(data.get("parent_op"), str):
        return "journal.json: parent_op is not a string"
    guards = data.get("guards", {})
    if not isinstance(guards, dict):
        return "journal.json: guards is not an object"
    for path, digest in guards.items():
        reason = _safe_relpath(path)
        if reason:
            return f"journal.json: guard {reason}"
        if path in seen:
            return f"journal.json: {path!r} is both a file and a guard"
        if digest is not None and not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
            return f"journal.json: guard {path!r} has a malformed digest"
    dirs = data.get("created_dirs", [])
    if not isinstance(dirs, list) or any(_safe_relpath(d) for d in dirs):
        return "journal.json: created_dirs is not a list of safe paths"
    if data.get("recovery_strategy") not in (None, "rollback", "forward"):
        return f"journal.json: unknown recovery_strategy {data.get('recovery_strategy')!r}"
    return None


def _check_preconditions(target: Path, files: List[FileEntry], guards: Optional[Dict[str, Optional[str]]] = None) -> None:
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
    for path, digest in (guards or {}).items():
        full = safe_join(target, path)
        _no_symlink_on_path(target, path)
        current = _digest(full)
        if current != digest:
            raise WriteError(f"concurrent change: {path}, which the plan read, changed after the plan was computed (expected {str(digest)[:12]}, found {str(current)[:12]}); plan again")


def _other_operations(target: Path, op_id: str) -> List[str]:
    """Pending or unreadable operations other than ``op_id``: a project with one of them is not written again."""
    out: List[str] = []
    for op in pending_operations(target):
        if op.op_id != op_id:
            out.append(f"{op.op_id} ({op.state})")
    for u in unreadable_operations(target):
        if u["op_id"] != op_id:
            out.append(f"{u['op_id']} (unreadable: {u['reason']})")
    return out


def prepare(target: Path, files: Dict[str, str], expected: Dict[str, Optional[str]], *, kind: str, description: str, durable: bool = True, steps: Optional[List[Dict[str, object]]] = None, parent_op: Optional[str] = None, guards: Optional[Dict[str, Optional[str]]] = None, faults: Optional[FaultInjector] = None) -> Operation:
    """Check every precondition, stage the new content and the backups, write the journal (state ``prepared``).
    Nothing in the project is modified. Raises WriteError/GenerationError and leaves no staging behind on failure.
    With ``durable`` (the default) every staged file, the journal and the directories that hold them are fsync'ed
    before returning, so an interruption after ``prepare`` finds a complete staging to recover from. Refuses while
    another operation of the project is pending or unreadable."""
    faults = faults or FaultInjector()
    faults.check("before_prepare")
    target = target.resolve()
    if target.is_symlink():
        raise GenerationError(f"the project is a symlink: {target}")
    for path in files:
        reason = _safe_relpath(path)
        if reason:
            raise GenerationError(f"refusing path outside the target: {path!r}")
    for path in guards or {}:
        if path in files:
            raise WriteError(f"{path} cannot be both written and guarded")
    op_id = operation_id(files, expected, kind, description)
    others = _other_operations(target, op_id)
    if others:
        raise WriteError("an interrupted operation exists: " + ", ".join(others) + "; run `evolve_fastapi.py recover` (or inspect .fastapi-architect/ops) before writing the project again")
    entries = [FileEntry(p, expected.get(p), sha256_bytes(files[p].encode("utf-8"))) for p in sorted(files)]
    op_dir = target / OPS_DIR / op_id
    if op_dir.exists():
        existing = load_operation(target, op_id)
        if existing is not None and existing.state != "prepared":
            raise WriteError(f"operation {op_id} is already {existing.state}; recover it before preparing it again")
    _check_preconditions(target, entries, guards)
    created_dirs = [d for d in (op_dir.parent, op_dir.parent.parent) if not d.exists()]  # removed again if staging fails
    if op_dir.exists():
        shutil.rmtree(op_dir, ignore_errors=True)
    op = Operation(op_id=op_id, kind=kind, description=description, files=entries, steps=[dict(s) for s in (steps or [])], parent_op=parent_op, guards=dict(guards or {}))
    try:
        for index, entry in enumerate(op.files, start=1):
            _write_atomic(op_dir / "new" / entry.path, files[entry.path].encode("utf-8"), durable=durable)
            if entry.expected is not None:
                _write_atomic(op_dir / "backup" / entry.path, (target / entry.path).read_bytes(), durable=durable)
            faults.check(f"after_stage:{index}")
        _save_journal(target, op, durable=durable)
        _sync_directories(target, op_dir, durable)
    except OSError as exc:
        shutil.rmtree(op_dir, ignore_errors=True)
        for directory in created_dirs:  # innermost first: nothing of ours is left behind
            try:
                directory.rmdir()
            except OSError:
                pass
        raise WriteError(f"could not stage the operation: {exc}") from exc
    return op


def _missing_ancestors(target: Path, full: Path) -> List[str]:
    """Directories under ``target`` that do not exist yet on the way to ``full`` (outermost first)."""
    out: List[str] = []
    current = full.parent
    while current != target and target in current.parents and not current.exists():
        out.append(current.relative_to(target).as_posix())
        current = current.parent
    return list(reversed(out))


def publish(target: Path, op: Operation, *, faults: Optional[FaultInjector] = None, durable: bool = True) -> List[str]:
    """Rename every staged file into place, journaling each landing; then clean up. Raises on interruption; the
    journal then describes exactly what landed (``recover`` finishes or undoes the rest). With ``durable`` (the
    default) each landed file, each journal state and the cleanup are fsync'ed as they happen. Every file's digest is
    re-checked immediately before its own rename: a file that changed meanwhile is recorded as refused and the
    publication stops there (the files already landed are the journal's to roll back)."""
    target = target.resolve()
    faults = faults or FaultInjector()
    op_dir = target / op.directory
    if op.state == "prepared":
        _check_preconditions(target, op.files, op.guards)  # the world may have moved since prepare
    faults.check("before_publish")
    op.state = "publishing"
    _save_journal(target, op, durable=durable)
    faults.check("after_journal")
    for index, entry in enumerate(op.files, start=1):
        if entry.path in op.published:
            continue
        full = safe_join(target, entry.path)
        _no_symlink_on_path(target, entry.path)
        current = _digest(full)
        if current != entry.expected and current != entry.new:
            op.conflicts[entry.path] = f"{NOT_WRITTEN} (expected {str(entry.expected)[:12]}, found {str(current)[:12]}); not written"
            _save_journal(target, op, durable=durable)
            raise WriteError(f"concurrent change during publication: {entry.path} changed after the plan was computed (expected {str(entry.expected)[:12]}, found {str(current)[:12]}); {len(op.published)} file(s) already landed: recover (rollback restores them, forward leaves this one alone)")
        staged = (op_dir / "new" / entry.path).read_bytes()
        if sha256_bytes(staged) != entry.new:
            op.conflicts[entry.path] = f"staged copy does not match the journal (found {sha256_bytes(staged)[:12]}, journal {entry.new[:12]}); not written"
            _save_journal(target, op, durable=durable)
            raise WriteError(f"staging of {entry.path} does not match the journal; nothing more is written: inspect .fastapi-architect/ops/{op.op_id}")
        new_dirs = [d for d in _missing_ancestors(target, full) if d not in op.created_dirs]
        if new_dirs:
            op.created_dirs.extend(new_dirs)
            _save_journal(target, op, durable=durable)
        _write_atomic(full, staged, durable=durable)
        op.published.append(entry.path)
        _save_journal(target, op, durable=durable)
        faults.check(f"after_publish:{index}")
    faults.check("after_publish_all")
    op.state = "published"
    _save_journal(target, op, durable=durable)
    faults.check("before_cleanup")
    complete(target, op, durable=durable, faults=faults)
    return [f.path for f in op.files]


def complete(target: Path, op: Operation, *, durable: bool = True, faults: Optional[FaultInjector] = None) -> None:
    """Record ``completed``, then remove the staging directory and the journal of a fully published (or fully
    recovered) operation; fsync'ed when ``durable``, so a completed operation does not come back as pending after a
    crash, and a crash during the cleanup itself is finished by the next reader instead of being mistaken for an
    interrupted publication."""
    faults = faults or FaultInjector()
    op_dir = target / op.directory
    if op.state != "completed" and (op_dir / JOURNAL).is_file():
        op.state = "completed"
        _save_journal(target, op, durable=durable)
    faults.check("during_cleanup")
    shutil.rmtree(op_dir, ignore_errors=True)
    ops = target / OPS_DIR
    if ops.is_dir() and not any(ops.iterdir()):
        ops.rmdir()
    if not ops.exists() and ops.parent.is_dir() and not any(ops.parent.iterdir()):
        ops.parent.rmdir()  # a fresh target that was rolled back: nothing of ours stays behind
    if durable:
        remaining = ops if ops.is_dir() else (ops.parent if ops.parent.is_dir() else target)
        if remaining.is_dir():
            fsync_directory(remaining)


def _read_journal(target: Path, op_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(document, reason)``: the parsed journal when it validates, otherwise the reason it does not."""
    path = Path(target).resolve() / OPS_DIR / op_id / JOURNAL
    if not path.is_file():
        return None, "journal.json is missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"journal.json is unreadable ({type(exc).__name__})"
    reason = validate_journal(data, op_id)
    if reason:
        return None, reason
    return data, None


def load_operation(target: Path, op_id: str) -> Optional[Operation]:
    """The operation of a directory under ``.fastapi-architect/ops`` when its journal validates; None otherwise
    (``unreadable_operations`` says why)."""
    data, _reason = _read_journal(target, op_id)
    if data is None:
        return None
    return Operation.from_dict(data)


def pending_operations(target: Path) -> List[Operation]:
    """Operations whose journal is still on disk: interrupted or awaiting attention. An operation whose journal says
    ``completed`` only lacks its cleanup: it is finished here (idempotent) and not reported."""
    root = Path(target).resolve()
    ops = root / OPS_DIR
    if not ops.is_dir():
        return []
    out: List[Operation] = []
    for child in sorted(ops.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        op = load_operation(root, child.name)
        if op is None:
            continue
        if op.state == "completed":
            complete(root, op, durable=True)
            continue
        out.append(op)
    return out


def unreadable_operations(target: Path) -> List[Dict[str, str]]:
    """Operation directories whose journal cannot be trusted (missing, truncated, not JSON, another schema, a
    document that does not validate): an interrupted write may hide behind each of them, so they block new plans and
    are reported for a human to inspect; nothing here guesses what happened or deletes anything (v2.0 acceptance).
    A stray file or symlink inside the operations directory is reported too."""
    root = Path(target).resolve()
    ops = root / OPS_DIR
    if not ops.is_dir():
        return []
    out: List[Dict[str, str]] = []
    for child in sorted(ops.iterdir()):
        if child.name == "LOCK" and child.is_file() and not child.is_symlink():
            continue
        if child.is_symlink():
            out.append({"op_id": child.name, "path": str(child), "reason": "a symlink inside the operations directory", "staged_files": "0"})
            continue
        if not child.is_dir():
            if child.name.endswith(TEMP_SUFFIX):
                continue  # a leftover temporary: residual_temporaries lists it
            out.append({"op_id": child.name, "path": str(child), "reason": "a stray file inside the operations directory", "staged_files": "0"})
            continue
        data, reason = _read_journal(root, child.name)
        if reason:
            staged = sum(1 for p in (child / "new").rglob("*") if p.is_file()) if (child / "new").is_dir() else 0
            out.append({"op_id": child.name, "path": str(child / JOURNAL), "reason": reason, "staged_files": str(staged)})
    return out


def residual_temporaries(target: Path) -> List[str]:
    """Leftover temporaries of the writer (``*.fa-tmp``) inside the project, the operations directory included: a
    write that died between its temporary file and its rename. Listed for inspection; never deleted here (the project
    file next to each one holds either the previous or the new content, never a partial one)."""
    root = Path(target).resolve()
    out: List[str] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name not in _SKIP_DIRS:
                    stack.append(child)
            elif child.name.endswith(TEMP_SUFFIX):
                out.append(child.relative_to(root).as_posix())
    return sorted(out)


def classify(target: Path, op: Operation) -> Dict[str, str]:
    """``path -> published | pending | conflict | unsafe`` by current content (``unsafe``: the path escapes the
    project or crosses a symlink; such a file is never touched)."""
    target = Path(target).resolve()
    out: Dict[str, str] = {}
    for entry in op.files:
        try:
            full = safe_join(target, entry.path)
            _no_symlink_on_path(target, entry.path)
        except GenerationError:
            out[entry.path] = "unsafe"
            continue
        current = _digest(full)
        if current == entry.new:
            out[entry.path] = "published"
        elif current == entry.expected:
            out[entry.path] = "pending"
        else:
            out[entry.path] = "conflict"
    return out


def _staging_matches(op_dir: Path, entry: FileEntry, kind: str) -> Optional[str]:
    """The reason the staged copy (``new``) or the backup of ``entry`` cannot be used, or None."""
    if kind == "new":
        path, expected = op_dir / "new" / entry.path, entry.new
    else:
        path, expected = op_dir / "backup" / entry.path, entry.expected
    current = _digest(path)
    if current is None:
        return f"the {kind} copy under {OPS_DIR}/{op_dir.name} is missing; left untouched"
    if current != expected:
        return f"the {kind} copy under {OPS_DIR}/{op_dir.name} does not match the journal (found {current[:12]}, journal {str(expected)[:12]}); left untouched"
    return None


def _remove_created_dirs(target: Path, op: Operation, report: RecoveryReport) -> None:
    for rel in sorted(op.created_dirs, key=lambda d: d.count("/"), reverse=True):
        try:
            directory = safe_join(target, rel)
        except GenerationError:
            continue
        try:
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
                report.removed_dirs.append(rel)
        except OSError:
            pass


def recover(target: Path, op: Operation, *, strategy: str = "rollback", faults: Optional[FaultInjector] = None, durable: bool = True, lock: bool = True, wait_s: float = 0.0) -> RecoveryReport:
    """Finish (``forward``) or undo (``rollback``) an interrupted operation. Files in conflict are never touched.
    Progress is journaled; calling it again continues or confirms (idempotent); an interrupted recovery resumes only
    with the strategy it started with. Durable like ``publish``. Holds the project lock while it runs (``LockHeld``
    when another operation holds it). The staged copies and backups are verified against the journal before they are
    used; a path that escapes the project or crosses a symlink is never touched."""
    if strategy not in ("rollback", "forward"):
        raise ValueError("strategy must be 'rollback' or 'forward'")
    if lock:
        with project_lock(target, kind="recover", description=f"{strategy} {op.op_id}", wait_s=wait_s):
            return recover(target, op, strategy=strategy, faults=faults, durable=durable, lock=False)
    target = Path(target).resolve()
    faults = faults or FaultInjector()
    op_dir = target / op.directory
    report = RecoveryReport(op_id=op.op_id, strategy=strategy)
    faults.check("before_recover")
    started_with = RECOVERING.get(op.state) or op.recovery_strategy
    if started_with and started_with != strategy and op.recovered:
        raise WriteError(f"operation {op.op_id} is being recovered with strategy {started_with!r} ({len(op.recovered)} file(s) already handled); resume with the same strategy, a different one would mix both outcomes")
    if op.state == "completed":  # only the cleanup was interrupted
        complete(target, op, durable=durable)
        report.state = "completed"
        return report
    if op.state == "prepared":  # nothing was published: discarding the staging is the whole recovery
        complete(target, op, durable=durable)
        report.state = "discarded"
        return report
    op.state = f"recovering_{strategy}"
    op.recovery_strategy = strategy
    _save_journal(target, op, durable=durable)
    handled = 0
    for entry in op.files:
        faults.check("during_classify")
        kind = classify(target, Operation(op_id=op.op_id, kind=op.kind, description=op.description, files=[entry]))[entry.path]  # re-read right before acting
        if kind == "unsafe":
            op.conflicts[entry.path] = "the path escapes the project or crosses a symlink; left untouched"
            report.conflicts[entry.path] = op.conflicts[entry.path]
            continue
        full = safe_join(target, entry.path)
        if kind == "conflict":
            refused = entry.path in op.conflicts and op.conflicts[entry.path].startswith(NOT_WRITTEN) and entry.path not in op.published
            if refused and strategy == "rollback":
                report.already_done.append(entry.path)  # never written by this operation: nothing to undo
            else:
                op.conflicts[entry.path] = f"content is neither the expected nor the new version (found {str(_digest(full))[:12]}); left untouched"
                report.conflicts[entry.path] = op.conflicts[entry.path]
                continue
        elif strategy == "rollback":
            if kind == "published":
                if entry.expected is None:
                    full.unlink()  # created by this operation and still exactly what we wrote
                    if durable:
                        fsync_directory(full.parent)
                else:
                    reason = _staging_matches(op_dir, entry, "backup")
                    if reason:
                        op.conflicts[entry.path] = reason
                        report.conflicts[entry.path] = reason
                        continue
                    _write_atomic(full, (op_dir / "backup" / entry.path).read_bytes(), durable=durable)
                report.restored.append(entry.path)
                handled += 1
            else:
                report.already_done.append(entry.path)
        else:
            if kind == "pending":
                reason = _staging_matches(op_dir, entry, "new")
                if reason:
                    op.conflicts[entry.path] = reason
                    report.conflicts[entry.path] = reason
                    continue
                new_dirs = [d for d in _missing_ancestors(target, full) if d not in op.created_dirs]
                op.created_dirs.extend(new_dirs)
                _write_atomic(full, (op_dir / "new" / entry.path).read_bytes(), durable=durable)
                report.published.append(entry.path)
                handled += 1
            else:
                report.already_done.append(entry.path)
        if entry.path in op.conflicts and entry.path not in report.conflicts:
            del op.conflicts[entry.path]  # a conflict resolved by the operator, or a refused file we leave alone
        if entry.path not in op.recovered:
            op.recovered.append(entry.path)
        _save_journal(target, op, durable=durable)
        if handled:
            faults.check(f"recover_after:{handled}")
    if report.conflicts:
        op.state = "needs_attention"
        _save_journal(target, op, durable=durable)
        report.state = "needs_attention"
        return report
    if strategy == "rollback":
        _remove_created_dirs(target, op, report)
    complete(target, op, durable=durable)
    report.state = "rolled_back" if strategy == "rollback" else "completed"
    return report


def write_recoverable(target: Path, files: Dict[str, str], expected: Dict[str, Optional[str]], *, kind: str, description: str, faults: Optional[FaultInjector] = None, durable: bool = True, steps: Optional[List[Dict[str, object]]] = None, parent_op: Optional[str] = None, lock: bool = True, wait_s: float = 0.0, guards: Optional[Dict[str, Optional[str]]] = None) -> Operation:
    """prepare + publish under the project lock; on failure the journal stays for ``recover``. Returns the operation
    (state as left). ``durable=False`` skips the fsyncs: for throwaway copies (verification) only, never for a
    project. ``LockHeld`` is raised before anything is staged when another operation holds the project. ``guards``
    are files the plan read but does not write: their digests must hold at prepare and right before publication."""
    if lock:
        with project_lock(target, kind=kind, description=description, wait_s=wait_s):
            return write_recoverable(target, files, expected, kind=kind, description=description, faults=faults, durable=durable, steps=steps, parent_op=parent_op, lock=False, guards=guards)
    op = prepare(target, files, expected, kind=kind, description=description, durable=durable, steps=steps, parent_op=parent_op, guards=guards, faults=faults)
    publish(target, op, faults=faults, durable=durable)
    return op
