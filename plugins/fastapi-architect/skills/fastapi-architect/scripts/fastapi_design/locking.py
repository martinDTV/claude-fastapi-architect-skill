"""One writer at a time per project (v2.2, increment 0): a lock file with the holder's identity.

The recoverable writer re-checks every expected digest right before it renames the first file, so two writers that
race are kept apart by content: the second one aborts with nothing published. That leaves a window between the
re-check and the renames, and it says nothing about *who* is writing. The lock closes the window and names the
holder.

``.fastapi-architect/ops/LOCK`` is created with ``O_EXCL`` (the filesystem's own atomic primitive) and holds a JSON
record: schema, pid, host, start time, the kind and description of the operation and a random token. Policy:

* **fail fast** by default: when the file exists, ``LockHeld`` is raised with the holder's record and the reason
  (``held`` when the holder is alive on this host or cannot be judged, ``stale`` when its pid is gone on this host
  or the record is older than ``stale_after_s`` on another host);
* **bounded wait** on request (``wait_s`` > 0): the acquirer polls every ``poll_s`` until the lock frees or the
  time runs out, then ``LockHeld``;
* a stale lock is **never broken silently**: ``acquire(..., break_stale=True)`` (what ``ops unlock`` does after
  showing the holder) replaces it and records who did it in the new record's ``broke``.

``release`` removes the lock only when the token matches (a process never releases another's lock) and removes the
operation directory when it is empty, as ``journal.complete`` does. A crash leaves the file behind: that is the
stale case, visible with ``inspect``.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

LOCK_SCHEMA = "fastapi-architect/lock/1.0"
LOCK_FILE = "LOCK"
OPS_DIR = ".fastapi-architect/ops"
DEFAULT_STALE_AFTER_S = 6 * 3600.0


class LockHeld(RuntimeError):
    """The project is locked by another operation. ``holder`` is its record, ``reason`` is ``held`` or ``stale``."""

    def __init__(self, path: Path, holder: Dict[str, Any], reason: str) -> None:
        self.path, self.holder, self.reason = path, holder, reason
        who = f"pid {holder.get('pid')} on {holder.get('host')}" if holder else "an unreadable record"
        what = f"{holder.get('kind')}: {holder.get('description')}" if holder and holder.get("kind") else "unknown operation"
        since = holder.get("started_at") if holder else None
        super().__init__(f"{path}: locked by {who} ({what}) since {since}; {reason}")


@dataclass
class Lock:
    """A held lock; use ``release`` or the ``project_lock`` context manager."""

    path: Path
    token: str
    record: Dict[str, Any] = field(default_factory=dict)

    def release(self) -> bool:
        """Remove the lock when the token matches; True when removed."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("token") != self.token:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        ops = self.path.parent
        try:
            if ops.is_dir() and not any(ops.iterdir()):
                ops.rmdir()
        except OSError:
            pass
        return True


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


def _age_s(record: Dict[str, Any]) -> Optional[float]:
    try:
        started = datetime.fromisoformat(str(record.get("started_at")))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def inspect(target: Path, *, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Optional[Dict[str, Any]]:
    """The holder's record with ``status`` (``held``, ``stale``, ``unreadable``) and ``age_s``, or None when free."""
    path = Path(target).resolve() / OPS_DIR / LOCK_FILE
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"status": "unreadable", "path": str(path)}
    age = _age_s(record)
    same_host = record.get("host") == socket.gethostname()
    alive = _pid_alive(int(record.get("pid", 0))) if same_host else None
    if same_host and alive is False:
        status = "stale"
    elif not same_host and age is not None and age > stale_after_s:
        status = "stale"
    else:
        status = "held"
    out = dict(record)
    out.update({"status": status, "age_s": None if age is None else round(age, 1), "path": str(path), "same_host": same_host, "holder_alive": alive})
    return out


def acquire(target: Path, *, kind: str, description: str = "", wait_s: float = 0.0, poll_s: float = 0.1, stale_after_s: float = DEFAULT_STALE_AFTER_S, break_stale: bool = False) -> Lock:
    """Take the project lock or raise ``LockHeld``. Fail-fast by default; ``wait_s`` bounds an optional wait;
    ``break_stale`` replaces a lock whose holder is judged gone (never a live one)."""
    target = Path(target).resolve()
    path = target / OPS_DIR / LOCK_FILE
    token = uuid.uuid4().hex
    record = {"schema": LOCK_SCHEMA, "pid": os.getpid(), "host": socket.gethostname(), "started_at": datetime.now(timezone.utc).isoformat(), "kind": kind, "description": description, "token": token}
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        path.parent.mkdir(parents=True, exist_ok=True)  # a releasing holder removes the empty directory: recreate it on every attempt
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileNotFoundError:
            continue  # the directory vanished between the mkdir and the open (a release raced us): try again
        except FileExistsError:
            holder = inspect(target, stale_after_s=stale_after_s) or {}
            status = holder.get("status", "held")
            if status in ("stale", "unreadable") and break_stale:
                previous = {k: holder.get(k) for k in ("pid", "host", "started_at", "kind", "description", "status")}
                try:
                    path.unlink()
                except OSError:
                    pass
                record["broke"] = previous
                continue
            if time.monotonic() < deadline:
                time.sleep(poll_s)
                continue
            raise LockHeld(path, holder, status)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return Lock(path=path, token=token, record=record)


class project_lock:
    """``with project_lock(target, kind="evolve", description=...) as lock:`` acquire on enter, release on exit."""

    def __init__(self, target: Path, *, kind: str, description: str = "", wait_s: float = 0.0, break_stale: bool = False) -> None:
        self._args = (Path(target), kind, description, wait_s, break_stale)
        self.lock: Optional[Lock] = None

    def __enter__(self) -> Lock:
        target, kind, description, wait_s, break_stale = self._args
        self.lock = acquire(target, kind=kind, description=description, wait_s=wait_s, break_stale=break_stale)
        return self.lock

    def __exit__(self, *exc: object) -> None:
        if self.lock is not None:
            self.lock.release()


def unlock_stale(target: Path, *, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Dict[str, Any]:
    """Remove a lock judged stale or unreadable and report it; a held lock is refused (``removed: False``)."""
    holder = inspect(target, stale_after_s=stale_after_s)
    if holder is None:
        return {"removed": False, "reason": "no lock"}
    if holder.get("status") == "held":
        return {"removed": False, "reason": "held", "holder": holder}
    path = Path(holder["path"])
    try:
        path.unlink()
    except OSError as exc:
        return {"removed": False, "reason": f"could not remove: {exc}", "holder": holder}
    ops = path.parent
    try:
        if ops.is_dir() and not any(ops.iterdir()):
            ops.rmdir()
    except OSError:
        pass
    return {"removed": True, "reason": holder.get("status"), "holder": holder}


__all__ = ["DEFAULT_STALE_AFTER_S", "LOCK_FILE", "LOCK_SCHEMA", "Lock", "LockHeld", "acquire", "inspect", "project_lock", "unlock_stale"]
