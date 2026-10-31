"""Filesystem helpers shared by the engines: digests, the symlink check, contract-relative paths and the atomic
write, durable on request.

Durability model of ``atomic_write(durable=True)`` (v2.1, increment 0):

* the content is written to a temporary sibling (``<name>.fa-tmp``), flushed and ``fsync``'ed, then renamed over the
  destination, then the directory entry is ``fsync``'ed: once the call returns, a crash or power loss leaves either the
  previous content or the new one, and on POSIX filesystems that honour ``fsync`` the new one is on stable storage;
* what is **not** guaranteed: on macOS ``fsync`` flushes to the drive but does not force the drive's own cache
  (``F_FULLFSYNC`` is not issued); on Windows directory entries cannot be synced; filesystems that refuse to sync a
  directory (some network and FUSE mounts answer ``EINVAL``/``ENOTSUP``) get the file synced and the entry not, and
  the call reports it by returning ``False``;
* a failure to sync the **file** is an error (``OSError``), never silently downgraded: the temporary file is removed
  and the destination is left as it was.
"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from typing import Optional

TEMP_SUFFIX = ".fa-tmp"
# errno values that mean "this platform or filesystem cannot sync a directory entry", not "the sync failed".
_DIRECTORY_SYNC_UNSUPPORTED = tuple(code for code in (getattr(errno, name, None) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "EPERM", "EACCES", "EISDIR", "EBADF")) if code is not None)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_path(path: Path) -> Optional[str]:
    """Digest of a regular file's bytes; ``None`` when the path is not a regular file."""
    if not Path(path).is_file():
        return None
    return sha256_bytes(Path(path).read_bytes())


def symlink_on_path(target: Path, relpath: str) -> Optional[Path]:
    """The first component of ``target/relpath`` that is a symlink, or ``None`` when none is."""
    current = Path(target)
    for part in relpath.split("/"):
        current = current / part
        if current.is_symlink():
            return current
    return None


def resolve_against(raw: str, source: Optional[str]) -> Path:
    """A relative path written in a contract is relative to the directory of the contract file it came from."""
    path = Path(raw)
    if not path.is_absolute() and source:
        path = Path(source).resolve().parent / path
    return path


def fsync_directory(directory: Path) -> bool:
    """``fsync`` a directory entry. ``True`` when synced; ``False`` when the platform (Windows) or the filesystem
    cannot sync directories. Any other error propagates."""
    if os.name != "posix":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(directory), flags)
    except OSError as exc:
        if exc.errno in _DIRECTORY_SYNC_UNSUPPORTED:
            return False
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _DIRECTORY_SYNC_UNSUPPORTED:
            return False
        raise
    finally:
        os.close(fd)
    return True


def atomic_write(path: Path, data: bytes, *, durable: bool = False) -> bool:
    """Write ``data`` to a temporary sibling and rename it into place: readers see the previous content or the new
    one, never a partial file. With ``durable`` the temporary file is synced before the rename and the directory
    entry after it (see the module docstring for what that does and does not guarantee). Returns whether the
    directory entry was synced (always ``False`` when not durable)."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + TEMP_SUFFIX)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, destination)
    if not durable:
        return False
    return fsync_directory(destination.parent)
