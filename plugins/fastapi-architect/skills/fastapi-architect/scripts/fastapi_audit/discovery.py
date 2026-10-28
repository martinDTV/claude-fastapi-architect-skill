"""File discovery and environment classification (read-only)."""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .model import ENV_APPLICATION, ENV_MIGRATION, ENV_TEST

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        "dist",
        "build",
        "site-packages",
    }
)
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024

# Test code is classified, never hidden by default. ``--exclude-tests`` opts out explicitly.
TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "__tests__"})
TEST_FILE_RE = re.compile(r"^(test_.*|.*_tests?|conftest)\.py$")
MIGRATION_PATH_RE = re.compile(r"(^|/)(alembic|migrations?)/versions/[^/]+\.py$")


def classify_environment(relpath: str) -> str:
    """Return ``test``, ``migration`` or ``application`` from the path alone."""
    posix = relpath.replace(os.sep, "/")
    parts = posix.split("/")
    if any(part in TEST_DIR_NAMES for part in parts[:-1]):
        return ENV_TEST
    if TEST_FILE_RE.match(parts[-1]):
        return ENV_TEST
    if MIGRATION_PATH_RE.search(posix):
        return ENV_MIGRATION
    return ENV_APPLICATION


def _is_test_path(relpath: str) -> bool:
    return classify_environment(relpath) == ENV_TEST


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def match_globs(relpath: str, globs: Iterable[str]) -> bool:
    """fnmatch against the posix relpath, with and without a leading slash."""
    posix = relpath.replace(os.sep, "/")
    for pattern in globs:
        if fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch("/" + posix, pattern):
            return True
        # allow directory-style patterns such as "app/legacy/" or "app/legacy"
        clean = pattern.rstrip("/")
        if clean and (posix == clean or posix.startswith(clean + "/")):
            return True
    return False


def _count_py_files(directory: str, excluded: Set[str], include_hidden: bool) -> int:
    """Count the .py files a pruned test directory would have contributed."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [
            d
            for d in sorted(dirnames)
            if d not in excluded
            and not d.endswith(".egg-info")
            and (include_hidden or not d.startswith("."))
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        count += sum(1 for fn in filenames if fn.endswith(".py"))
    return count


def iter_python_files(
    root: str,
    *,
    excluded_dirs: Iterable[str] = (),
    include_hidden: bool = False,
    exclude_tests: bool = False,
    tests_excluded: Optional[List[int]] = None,
    include_globs: Iterable[str] = (),
    exclude_globs: Iterable[str] = (),
) -> Tuple[List[Tuple[str, str]], List[Dict[str, str]]]:
    """Return ``([(relpath, abspath), ...], [skipped, ...])`` deterministically.

    Symlinked directories are never followed. Symlinked files are analysed
    only when they resolve inside ``root``; a file reachable through several
    paths is analysed once, preferring the non-symlink path. With
    ``exclude_tests`` test directories and test-named files are not analysed;
    the number left out is appended to ``tests_excluded`` when a list is given.
    ``include_globs``/``exclude_globs`` filter relative paths (fnmatch).
    """
    excluded = set(DEFAULT_EXCLUDED_DIRS) | set(excluded_dirs)
    include_globs = tuple(include_globs)
    exclude_globs = tuple(exclude_globs)
    root_abs = os.path.abspath(root)
    root_real = os.path.realpath(root_abs)
    candidates: List[Tuple[bool, str, str, str]] = []  # (is_link, rel, abs, real)
    skipped: List[Dict[str, str]] = []
    left_out = 0

    def wanted(rel: str) -> bool:
        if include_globs and not match_globs(rel, include_globs):
            return False
        if exclude_globs and match_globs(rel, exclude_globs):
            return False
        return True

    if os.path.isfile(root_abs):
        rel = os.path.basename(root_abs)
        if exclude_tests and _is_test_path(rel):
            if tests_excluded is not None:
                tests_excluded.append(1)
            return [], skipped
        if not wanted(rel):
            return [], skipped
        return [(rel, root_abs)], skipped

    def record_walk_error(error: OSError) -> None:
        # A directory that cannot be listed makes the analysis partial; record it
        # instead of letting os.walk drop it silently.
        failed = getattr(error, "filename", None) or root_abs
        rel = os.path.relpath(failed, root_abs).replace(os.sep, "/")
        skipped.append({"file": rel, "reason": "unreadable_directory"})

    for dirpath, dirnames, filenames in os.walk(
        root_abs, onerror=record_walk_error, followlinks=False
    ):
        kept = []
        for name in sorted(dirnames):
            if name in excluded or name.endswith(".egg-info"):
                continue
            if not include_hidden and name.startswith("."):
                continue
            if os.path.islink(os.path.join(dirpath, name)):
                continue
            if exclude_tests and name in TEST_DIR_NAMES:
                left_out += _count_py_files(os.path.join(dirpath, name), excluded, include_hidden)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if exclude_tests and TEST_FILE_RE.match(name):
                left_out += 1
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
            if not wanted(rel):
                continue
            is_link = os.path.islink(full)
            real = os.path.realpath(full)
            if is_link and not _is_within(real, root_real):
                skipped.append({"file": rel, "reason": "symlink_outside_root"})
                continue
            candidates.append((is_link, rel, full, real))

    seen: Set[str] = set()
    files: List[Tuple[str, str]] = []
    for is_link, rel, full, real in sorted(candidates):
        if real in seen:
            skipped.append({"file": rel, "reason": "duplicate_of_scanned_file"})
            continue
        seen.add(real)
        files.append((rel, full))
    files.sort(key=lambda item: item[0])
    skipped.sort(key=lambda item: (item["file"], item["reason"]))
    if tests_excluded is not None:
        tests_excluded.append(left_out)
    return files, skipped
