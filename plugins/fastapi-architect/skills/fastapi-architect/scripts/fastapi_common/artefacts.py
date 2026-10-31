"""Transient artefacts: what tools leave behind and no engine should mistake for project content.

One definition, four consumers: the audit file walker (``fastapi_audit.discovery.DEFAULT_EXCLUDED_DIRS``), the
verification workspace copy (``fastapi_audit.remediate.isolation.DEFAULT_IGNORED_DIRS``), the design engine and the
test suite (snapshot comparisons, tree digests). Each consumer documents the extra names it adds on top.

Deliberately **not** listed here, so that legitimate files are never hidden:

* configuration directories (``.vscode``, ``.idea``, ``.github``): they are content;
* ``env``: a common virtual-environment name that is also a plausible package name; only the workspace copy
  excludes it, as it has since v1.9, and the audit keeps scanning it;
* ``*.fa-tmp``: the temporary sibling of an interrupted atomic write is evidence, never noise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

# Version control, bytecode, tool caches and build output.
TRANSIENT_DIRS: Tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    ".eggs",
    "dist",
    "build",
    "node_modules",
)
# Virtual environments by their conventional names.
VIRTUALENV_DIRS: Tuple[str, ...] = (".venv", "venv")
# Files the operating system or a file manager drops into any directory.
TRANSIENT_FILES: Tuple[str, ...] = (".DS_Store", "Thumbs.db", "desktop.ini")
# Compiled bytecode outside a cache directory.
TRANSIENT_SUFFIXES: Tuple[str, ...] = (".pyc", ".pyo")
# What a tree walk skips by default: everything above except the virtual environments are also skipped, because
# a generated or audited project never ships one and a copy or snapshot must not depend on whether tests were run.
DEFAULT_WALK_SKIP: Tuple[str, ...] = TRANSIENT_DIRS + VIRTUALENV_DIRS


def is_transient_dir(name: str, skip_dirs: Sequence[str] = DEFAULT_WALK_SKIP) -> bool:
    return name in skip_dirs


def is_transient_file(name: str, skip_files: Sequence[str] = TRANSIENT_FILES, skip_suffixes: Sequence[str] = TRANSIENT_SUFFIXES) -> bool:
    return name in skip_files or name.endswith(tuple(skip_suffixes))


def is_transient(parts: Iterable[str], *, skip_dirs: Sequence[str] = DEFAULT_WALK_SKIP, skip_files: Sequence[str] = TRANSIENT_FILES, skip_suffixes: Sequence[str] = TRANSIENT_SUFFIXES) -> bool:
    """Whether a relative path (given as its parts) lies under a transient directory or names a transient file."""
    items = list(parts)
    if not items:
        return False
    if any(is_transient_dir(p, skip_dirs) for p in items[:-1]):
        return True
    last = items[-1]
    return is_transient_dir(last, skip_dirs) or is_transient_file(last, skip_files, skip_suffixes)


def walk_files(root: Path, *, skip_dirs: Sequence[str] = DEFAULT_WALK_SKIP, skip_files: Sequence[str] = TRANSIENT_FILES, skip_suffixes: Sequence[str] = TRANSIENT_SUFFIXES) -> List[Path]:
    """Every regular file under ``root`` that is not transient, sorted by relative POSIX path. Transient directories
    are pruned (never entered); symlinked directories are not followed; symlinked files are listed as files."""
    base = Path(root)
    found: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not is_transient_dir(d, skip_dirs))
        for name in filenames:
            if is_transient_file(name, skip_files, skip_suffixes):
                continue
            path = Path(dirpath) / name
            if path.is_file():
                found.append((path.relative_to(base).as_posix(), path))
    found.sort(key=lambda item: item[0])
    return [path for _rel, path in found]


def relative_files(root: Path, **kwargs: Sequence[str]) -> List[str]:
    """``walk_files`` as sorted relative POSIX paths."""
    base = Path(root)
    return [p.relative_to(base).as_posix() for p in walk_files(base, **kwargs)]
