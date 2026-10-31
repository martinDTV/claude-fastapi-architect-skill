"""Shared infrastructure of FastAPI Architect (v2.1, increment 0).

Three things that every engine needs and none should define twice:

* ``version``: the plugin version, declared once (``fastapi_audit.model.TOOL_VERSION`` re-exports it);
* ``artefacts``: the transient directories and files (tool caches, bytecode, Finder droppings, virtual
  environments) that no audit, copy, comparison or snapshot should treat as part of a project;
* ``fs``: digests, the symlink check, contract-relative path resolution and the atomic write, durable on request.

Standard library only. ``fastapi_audit`` and ``fastapi_design`` import this package; it imports neither of them.
"""

from .artefacts import (
    DEFAULT_WALK_SKIP,
    TRANSIENT_DIRS,
    TRANSIENT_FILES,
    TRANSIENT_SUFFIXES,
    VIRTUALENV_DIRS,
    is_transient,
    is_transient_dir,
    is_transient_file,
    relative_files,
    walk_files,
)
from .fs import atomic_write, fsync_directory, resolve_against, sha256_bytes, sha256_path, sha256_text, symlink_on_path
from .version import TOOL_VERSION

__all__ = [
    "DEFAULT_WALK_SKIP",
    "TOOL_VERSION",
    "TRANSIENT_DIRS",
    "TRANSIENT_FILES",
    "TRANSIENT_SUFFIXES",
    "VIRTUALENV_DIRS",
    "atomic_write",
    "fsync_directory",
    "is_transient",
    "is_transient_dir",
    "is_transient_file",
    "relative_files",
    "resolve_against",
    "sha256_bytes",
    "sha256_path",
    "sha256_text",
    "symlink_on_path",
    "walk_files",
]
