"""One writer at a time per project (v2.2, increment 0): a lock file with the holder's identity.

The recoverable writer re-checks every expected digest right before it renames the first file, so two writers that
race are kept apart by content: the second one aborts with nothing published. That leaves a window between the
re-check and the renames, and it says nothing about *who* is writing. The lock closes the window and names the
holder.

``.fastapi-architect/ops/LOCK`` holds a JSON record: schema, pid, host, start time, the kind and description of the
operation and a random token. It is created atomically with its content (the record is written to a temporary
sibling and hard-linked into place; the link fails when the lock exists; where hard links are not available the
file is created with ``O_EXCL`` and written at once), so a reader never sees an empty or partial record of a live
holder. Policy:

* **fail fast** by default: when the file exists, ``LockHeld`` is raised with the holder's record and the reason:
  ``held`` (the holder is alive on this host), ``stale`` (its pid is gone on this host), ``unknown`` (another host,
  or a pid this host cannot judge: nobody here can tell whether the operation continues), ``unreadable`` (the
  record is not a valid JSON object) or ``invalid`` (the lock path is a symlink or a directory);
* **bounded wait** on request (``wait_s`` > 0): the acquirer polls every ``poll_s`` until the lock frees or the
  time runs out, then ``LockHeld``;
* a lock is **never broken silently**: ``acquire(..., break_stale=True)`` (what ``ops unlock`` does after showing
  the holder) replaces one judged ``stale`` or ``unreadable`` and records who did it in the new record's ``broke``;
  a lock of **another host is never broken by age**: its age is reported, and only an operator who names that host
  (``ops unlock --other-host NAME``, ``unlock_stale(other_host=NAME)``) removes it; an ``invalid`` lock is removed
  by hand only.

``release`` removes the lock only when the token matches (a process never releases another's lock) and removes the
operation directory when it is empty, as ``journal.complete`` does. A crash leaves the file behind: that is the
stale case, visible with ``inspect``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

LOCK_SCHEMA = "fastapi-architect/lock/1.0"
LOCK_FILE = "LOCK"
OPS_DIR = ".fastapi-architect/ops"
DEFAULT_STALE_AFTER_S = 6 * 3600.0  # reported as age information for locks of other hosts; never a reason to break them
STATUSES = ("held", "stale", "unknown", "unreadable", "invalid")
_TEMP_SUFFIX = ".fa-tmp"


class LockHeld(RuntimeError):
    """The project is locked by another operation. ``holder`` is its record, ``reason`` is one of ``STATUSES``."""

    def __init__(self, path: Path, holder: Dict[str, Any], reason: str) -> None:
        self.path, self.holder, self.reason = path, holder, reason
        who = f"pid {holder.get('pid')} on {holder.get('host')}" if holder and holder.get("pid") is not None else "an unreadable record"
        what = f"{holder.get('kind')}: {holder.get('description')}" if holder and holder.get("kind") else "unknown operation"
        since = holder.get("started_at") if holder else None
        hint = {"held": "wait for it to finish", "stale": "its process is gone on this host (or its pid now belongs to another process): `ops unlock` removes it", "unknown": "it belongs to another host or cannot be judged here: confirm it is not running, then `ops unlock --other-host <host>`", "unreadable": "the record is not readable: inspect it, then `ops unlock`", "invalid": "the lock path is not a regular file: remove it by hand after inspecting it"}.get(reason, reason)
        super().__init__(f"{path}: locked by {who} ({what}) since {since}; {reason}: {hint}")


@dataclass
class Lock:
    """A held lock; use ``release`` or the ``project_lock`` context manager."""

    path: Path
    token: str
    record: Dict[str, Any] = field(default_factory=dict)

    def release(self) -> bool:
        """Remove the lock when the token matches; True when removed (False: it was broken or replaced by someone else)."""
        try:
            if self.path.is_symlink():
                return False
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("token") != self.token:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        _remove_empty(self.path.parent)
        return True


def _remove_empty(ops: Path) -> None:
    """Remove the operations directory when it is empty and, after it, an empty ``.fastapi-architect`` (a fresh
    target that was rolled back keeps nothing of ours)."""
    for directory in (ops, ops.parent):
        try:
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
            else:
                return
        except OSError:
            return


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False when it can be judged on this host, None otherwise."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _process_start(pid: int) -> Optional[datetime]:
    """When process ``pid`` started on this host (``ps -o lstart``, second resolution), or None when it cannot be told.
    Used to recognise a pid that the system reused after the holder died: the lock then belongs to nobody."""
    if pid <= 0 or os.name != "posix":
        return None
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    for pattern in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return datetime.strptime(" ".join(text.split()), pattern).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _pid_reused(record: Dict[str, Any]) -> Optional[bool]:
    """True when the alive pid of the record is not the process that took the lock: its start time differs from the
    one the record carries (``process_started_at``), or, for records without it, the process started after the lock
    was taken. None when nothing can be told."""
    current = _process_start(int(record["pid"]))
    if current is None:
        return None
    recorded = record.get("process_started_at") or record.get("started_at")
    try:
        reference = datetime.fromisoformat(str(recorded))
    except (TypeError, ValueError):
        return None
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if record.get("process_started_at"):
        return abs((current - reference).total_seconds()) > 2.0
    return (current - reference).total_seconds() > 2.0


def _age_s(record: Dict[str, Any]) -> Optional[float]:
    try:
        started = datetime.fromisoformat(str(record.get("started_at")))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    """The holder's record when it is a JSON object with an integer pid and a string host; None otherwise."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int) or isinstance(record.get("pid"), bool) or not isinstance(record.get("host"), str) or not record.get("host"):
        return None
    return record


def inspect(target: Path, *, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Optional[Dict[str, Any]]:
    """The holder's record with ``status`` (one of ``STATUSES``), ``age_s``, ``same_host``, ``holder_alive`` and,
    for another host, ``older_than_stale_after`` (information, not a verdict), or None when the project is free."""
    path = Path(target).resolve() / OPS_DIR / LOCK_FILE
    if path.is_symlink() or path.is_dir():
        return {"status": "invalid", "path": str(path), "reason": "the lock path is a symlink or a directory"}
    if not path.exists():
        return None
    record = _read_record(path)
    if record is None:
        return {"status": "unreadable", "path": str(path)}
    age = _age_s(record)
    same_host = record.get("host") == socket.gethostname()
    alive = _pid_alive(int(record["pid"])) if same_host else None
    reused = _pid_reused(record) if same_host and alive is True else None
    if same_host and alive is True and not reused:
        status = "held"
    elif same_host and (alive is False or reused):
        status = "stale"
    else:
        status = "unknown"
    out = dict(record)
    out.update({"status": status, "age_s": None if age is None else round(age, 1), "path": str(path), "same_host": same_host, "holder_alive": alive, "pid_reused": reused})
    if not same_host:
        out["older_than_stale_after"] = age is not None and age > stale_after_s
    return out


def _create(path: Path, record: Dict[str, Any]) -> bool:
    """Create the lock file with its full content in one visible step. True when created, False when it exists."""
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f"{LOCK_FILE}.{record['token']}{_TEMP_SUFFIX}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no hard links here (some filesystems, Windows): fall back to O_EXCL and an immediate write
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def acquire(target: Path, *, kind: str, description: str = "", wait_s: float = 0.0, poll_s: float = 0.1, stale_after_s: float = DEFAULT_STALE_AFTER_S, break_stale: bool = False, break_other_host: Optional[str] = None) -> Lock:
    """Take the project lock or raise ``LockHeld``. Fail-fast by default; ``wait_s`` bounds an optional wait;
    ``break_stale`` replaces a lock whose holder is judged gone on this host or whose record is unreadable (never a
    live one, never one of another host); ``break_other_host`` names the host whose lock the operator confirmed
    abandoned (an ``unknown`` lock of exactly that host is replaced)."""
    target = Path(target).resolve()
    path = target / OPS_DIR / LOCK_FILE
    token = uuid.uuid4().hex
    own_start = _process_start(os.getpid())
    record = {"schema": LOCK_SCHEMA, "pid": os.getpid(), "host": socket.gethostname(), "started_at": datetime.now(timezone.utc).isoformat(), "kind": kind, "description": description, "token": token}
    if own_start is not None:
        record["process_started_at"] = own_start.isoformat()  # lets a reader tell this process from a later one that got the same pid
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        path.parent.mkdir(parents=True, exist_ok=True)  # a releasing holder removes the empty directory: recreate it on every attempt
        try:
            created = _create(path, record)
        except FileNotFoundError:
            continue  # the directory vanished between the mkdir and the open (a release raced us): try again
        if created:
            return Lock(path=path, token=token, record=record)
        holder = inspect(target, stale_after_s=stale_after_s) or {}
        status = holder.get("status", "held")
        breakable = (status in ("stale", "unreadable") and break_stale) or (status == "unknown" and break_other_host is not None and holder.get("host") == break_other_host and not holder.get("same_host"))
        if breakable:
            previous = {k: holder.get(k) for k in ("pid", "host", "started_at", "kind", "description", "status")}
            try:
                path.unlink()
            except OSError:
                pass
            record["broke"] = previous
            continue
        if status in ("held", "unknown") and time.monotonic() < deadline:
            time.sleep(poll_s)
            continue
        raise LockHeld(path, holder, status)


class project_lock:
    """``with project_lock(target, kind="evolve", description=...) as lock:`` acquire on enter, release on exit."""

    def __init__(self, target: Path, *, kind: str, description: str = "", wait_s: float = 0.0, break_stale: bool = False) -> None:
        self._args = (Path(target), kind, description, wait_s, break_stale)
        self.lock: Optional[Lock] = None
        self.released: Optional[bool] = None  # False after exit means someone else broke or replaced our lock meanwhile

    def __enter__(self) -> Lock:
        target, kind, description, wait_s, break_stale = self._args
        self.lock = acquire(target, kind=kind, description=description, wait_s=wait_s, break_stale=break_stale)
        return self.lock

    def __exit__(self, *exc: object) -> None:
        if self.lock is not None:
            self.released = self.lock.release()


def unlock_stale(target: Path, *, stale_after_s: float = DEFAULT_STALE_AFTER_S, other_host: Optional[str] = None) -> Dict[str, Any]:
    """Remove a lock judged ``stale`` or ``unreadable`` and report it. A ``held`` lock is refused; an ``unknown`` lock
    (another host, or a pid this host cannot judge) is removed only when ``other_host`` names its host exactly; an
    ``invalid`` lock (symlink or directory) is never removed here."""
    holder = inspect(target, stale_after_s=stale_after_s)
    if holder is None:
        return {"removed": False, "reason": "no lock"}
    status = holder.get("status")
    if status == "held":
        return {"removed": False, "reason": "held", "holder": holder}
    if status == "invalid":
        return {"removed": False, "reason": "invalid: the lock path is a symlink or a directory; inspect and remove it by hand", "holder": holder}
    if status == "unknown":
        if holder.get("same_host"):
            return {"removed": False, "reason": "unknown: the holder's pid cannot be judged on this host; confirm the process is gone and remove the lock by hand", "holder": holder}
        if other_host is None or other_host != holder.get("host"):
            return {"removed": False, "reason": f"unknown: the lock belongs to host {holder.get('host')!r} (age {holder.get('age_s')} s); nothing here can tell whether that operation continues; confirm it on that host, then pass --other-host {holder.get('host')}", "holder": holder}
    path = Path(holder["path"])
    try:
        path.unlink()
    except OSError as exc:
        return {"removed": False, "reason": f"could not remove: {exc}", "holder": holder}
    _remove_empty(path.parent)
    return {"removed": True, "reason": status if status != "unknown" else f"unknown, confirmed by the operator for host {other_host}", "holder": holder}


__all__ = ["DEFAULT_STALE_AFTER_S", "LOCK_FILE", "LOCK_SCHEMA", "STATUSES", "Lock", "LockHeld", "acquire", "inspect", "project_lock", "unlock_stale"]
