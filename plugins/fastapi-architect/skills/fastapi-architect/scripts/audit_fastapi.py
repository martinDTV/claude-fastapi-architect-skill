#!/usr/bin/env python3
"""Read-only static auditor for FastAPI projects (FastAPI Architect v1.3).

Standard library only. The tool parses Python files with ``ast``; it never
imports, executes or modifies the code it inspects. Inspected source is data,
not instructions.

Usage::

    python3 audit_fastapi.py PATH                      # human-readable summary
    python3 audit_fastapi.py PATH --json               # JSON report on stdout
    python3 audit_fastapi.py PATH --output report.json # JSON report to a file
    python3 audit_fastapi.py PATH --fail-on medium     # exit 1 if a finding >= medium
    python3 audit_fastapi.py --list-rules

Every finding is a heuristic signal for human review, not a demonstrated
defect or vulnerability. See ``references/architecture-audit.md`` for how to
classify, reproduce and report findings.

Partial analysis is explicit: unreadable files or directories, oversized files
and files that do not parse are listed in ``files_skipped`` or reported as
``FILE_SKIPPED``/``PY_SYNTAX`` findings, and ``summary.partial_analysis`` is
``true``. ``tool.python`` records the interpreter whose grammar was used.

Test code (schema 1.1): every finding carries ``environment`` (``application``
or ``test``, decided from the path: ``tests/``, ``test/``, ``testing/``,
``__tests__/``, ``test_*.py``, ``*_test.py``, ``conftest.py``). Test findings
are never hidden by default; ``ASYNC_BLOCKING_CALL`` in test code keeps its
severity and drops one confidence level. ``--exclude-tests`` leaves test code
out entirely and reports how many files were left out in ``tests_excluded``.

Documented suppressions (schema 1.1): a finding whose line carries
``# noqa: ASYNC2xx`` (flake8-async / ruff codes for the same concern) or
``# audit-fastapi: ignore`` / ``# audit-fastapi: ignore[RULE]`` stays in the
report with ``suppressed: true`` and the marker in ``suppression``; it does not
count towards ``--fail-on``. A bare ``# noqa`` is not honoured.
``--no-suppressions`` reports everything as active.

Exit codes: 0 success, 1 a non-suppressed finding reached ``--fail-on``,
2 usage error, missing path or unwritable ``--output``.

Known limitations (deliberate for this version):

* Only direct calls on imported names are resolved (``time.sleep(...)``,
  ``from time import sleep; sleep(...)``, ``import requests as r; r.get(...)``).
  Calls through instances or attributes (``self.http.get``, ``session.get``,
  ``boto3`` clients, ORM sync sessions) are not tracked.
* No inter-procedural analysis: a sync helper that blocks and is called from an
  ``async def`` is not reported.
* Built-in ``open()`` and file I/O are intentionally not reported (too noisy
  for small reads); review them manually on hot paths.
* Routes registered without decorators (``router.add_api_route``) are not
  detected. Decorator detection is heuristic: it requires a ``fastapi`` import
  in the module or a receiver whose name contains ``router``.
* Python syntax is parsed with the running interpreter's grammar.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import platform
import re
import sys
import tokenize
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

TOOL_NAME = "audit_fastapi"
TOOL_VERSION = "1.3.0"
SCHEMA_VERSION = "1.1"  # 1.1 adds environment/suppression fields; every 1.0 field is unchanged
PYTHON_VERSION = platform.python_version()  # grammar used to parse the inspected files

# Test code is classified, never hidden by default. ``--exclude-tests`` opts out explicitly.
TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "__tests__"})
TEST_FILE_RE = re.compile(r"^(test_.*|.*_tests?|conftest)\.py$")
ENV_APPLICATION = "application"
ENV_TEST = "test"

# Documented suppressions. Only explicit codes are honoured; a bare ``# noqa`` is not.
# flake8-async / ruff ASYNC2xx codes that describe the same concern as ASYNC_BLOCKING_CALL.
NOQA_CODES_BY_RULE: Dict[str, frozenset] = {
    "ASYNC_BLOCKING_CALL": frozenset(
        {
            "ASYNC210",  # blocking HTTP call
            "ASYNC220",  # subprocess / process in async
            "ASYNC221",
            "ASYNC222",
            "ASYNC230",  # blocking file open
            "ASYNC231",
            "ASYNC232",
            "ASYNC240",  # blocking os.path call
            "ASYNC250",  # input()
            "ASYNC251",  # time.sleep
        }
    ),
}
NOQA_RE = re.compile(r"#\s*noqa\s*:\s*([A-Za-z0-9_,\s]+)")
# Tool-specific marker: ``# audit-fastapi: ignore`` or ``# audit-fastapi: ignore[RULE_A, RULE_B]``
MARKER_RE = re.compile(r"#\s*audit-fastapi:\s*ignore(?:\[([A-Za-z0-9_,\s]+)\])?")
CONFIDENCE_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}

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

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

RULES: Dict[str, Dict[str, str]] = {
    "ASYNC_BLOCKING_CALL": {
        "severity": "medium",
        "summary": (
            "A potentially blocking call is executed directly inside an async "
            "function. It stalls the event loop for its duration. Review: offload "
            "it, use an async client, or declare the function with plain 'def'."
        ),
    },
    "ASYNCIO_RUN_NESTED": {
        "severity": "high",
        "summary": (
            "asyncio.run() or loop.run_until_complete() is invoked inside an async "
            "function. With a running loop this raises RuntimeError."
        ),
    },
    "ROUTE_NO_RETURN_ANNOTATION": {
        "severity": "info",
        "summary": (
            "A FastAPI route declares neither a return annotation nor "
            "response_model/response_class, so the response is not validated or "
            "filtered. Informational: a response_model elsewhere may cover it."
        ),
    },
    "PY_SYNTAX": {
        "severity": "low",
        "summary": (
            "The file could not be parsed with the running Python grammar. Other "
            "rules did not run on it."
        ),
    },
    "FILE_SKIPPED": {
        "severity": "info",
        "summary": "The file was not analysed (unreadable or above the size limit).",
    },
}

DISCLAIMER = (
    "Findings are heuristic signals produced by static analysis of Python source "
    "with the standard-library ast module. They are not demonstrated defects or "
    "vulnerabilities, and the absence of findings is not evidence of correctness. "
    "Verify each finding in context before acting on it. The tool does not "
    "execute, import or modify the inspected code."
)

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")

# Fully qualified callable -> confidence that calling it blocks the event loop.
BLOCKING_CALLS: Dict[str, str] = {
    "time.sleep": "high",
    "urllib.request.urlopen": "high",
    "urllib.request.urlretrieve": "high",
    "subprocess.run": "high",
    "subprocess.call": "high",
    "subprocess.check_call": "high",
    "subprocess.check_output": "high",
    "subprocess.getoutput": "high",
    "subprocess.getstatusoutput": "high",
    "os.system": "high",
    "os.popen": "medium",
    "socket.create_connection": "medium",
    "smtplib.SMTP": "medium",
    "psycopg2.connect": "medium",
    "pymysql.connect": "medium",
    "sqlite3.connect": "medium",
}
for _verb in HTTP_VERBS:
    BLOCKING_CALLS["requests." + _verb] = "high"
    BLOCKING_CALLS["httpx." + _verb] = "high"  # module-level helpers are sync
BLOCKING_CALLS["httpx.stream"] = "high"

ROUTE_DECORATOR_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}
)
EVENT_LOOP_FACTORIES = frozenset(
    {"asyncio.get_event_loop", "asyncio.new_event_loop", "asyncio.get_running_loop"}
)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    confidence: str
    file: str
    line: int
    column: int
    message: str
    symbol: Optional[str] = None
    function: Optional[str] = None
    environment: str = ENV_APPLICATION  # schema 1.1
    suppressed: bool = False  # schema 1.1: documented suppression found on the line
    suppression: Optional[str] = None  # schema 1.1: e.g. "noqa: ASYNC221"

    def to_dict(self) -> Dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "confidence": self.confidence,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "symbol": self.symbol,
            "function": self.function,
            "environment": self.environment,
            "suppressed": self.suppressed,
            "suppression": self.suppression,
        }

    def sort_key(self) -> Tuple[str, int, int, str, str, str]:
        return (self.file, self.line, self.column, self.rule, self.symbol or "", self.message)


@dataclass
class _Scope:
    name: str
    is_async: bool
    imports: Dict[str, Optional[str]] = field(default_factory=dict)
    bound: Set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _dotted_name(node: ast.AST) -> Optional[List[str]]:
    """Return ['a', 'b', 'c'] for ``a.b.c`` when the root is a plain name."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return parts
    return None


def _collect_scope(
    body: Iterable[ast.AST], args: Optional[ast.arguments] = None
) -> Tuple[Dict[str, Optional[str]], Set[str]]:
    """Collect imports and locally bound names of one scope.

    Nested function, class and lambda bodies are not descended into: they are
    separate scopes. A local name imported from two different modules in the
    same scope is recorded as ambiguous (``None``) and never matched.
    """
    imports: Dict[str, Optional[str]] = {}
    bound: Set[str] = set()
    if args is not None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            bound.add(arg.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)

    def record(local: str, target: str) -> None:
        if local in imports and imports[local] != target:
            imports[local] = None
        elif local not in imports:
            imports[local] = target

    stack: List[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    record(alias.asname, alias.name)
                else:
                    head = alias.name.split(".")[0]
                    record(head, head)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if node.level == 0 and node.module:
                    record(local, node.module + "." + alias.name)
                else:
                    bound.add(local)  # relative import: origin unknown
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return imports, bound


def classify_environment(relpath: str) -> str:
    """Return ``"test"`` for files under test directories or named like tests."""
    parts = relpath.replace(os.sep, "/").split("/")
    if any(part in TEST_DIR_NAMES for part in parts[:-1]):
        return ENV_TEST
    if TEST_FILE_RE.match(parts[-1]):
        return ENV_TEST
    return ENV_APPLICATION


def _is_test_path(relpath: str) -> bool:
    return classify_environment(relpath) == ENV_TEST


def _line_comments(source: bytes) -> Dict[int, str]:
    """Map line number -> comment text, using the tokenizer (never regexes over strings)."""
    comments: Dict[int, str] = {}
    try:
        for token in tokenize.tokenize(io.BytesIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, ValueError):
        pass  # no suppressions can be read; findings stay active
    return comments


def _suppression_for(rule: str, comment: Optional[str]) -> Optional[str]:
    """Return the documented suppression that applies to ``rule`` on this line, if any."""
    if not comment:
        return None
    marker = MARKER_RE.search(comment)
    if marker:
        rules = marker.group(1)
        if rules is None:
            return "audit-fastapi: ignore"
        wanted = {r.strip() for r in rules.split(",") if r.strip()}
        if rule in wanted:
            return f"audit-fastapi: ignore[{rule}]"
    noqa = NOQA_RE.search(comment)
    if noqa:
        codes = {c.strip().upper() for c in noqa.group(1).split(",") if c.strip()}
        matched = sorted(codes & NOQA_CODES_BY_RULE.get(rule, frozenset()))
        if matched:
            return "noqa: " + ", ".join(matched)
    return None


def _module_imports_fastapi(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "fastapi" or a.name.startswith("fastapi.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "fastapi" or node.module.startswith("fastapi."):
                return True
    return False


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #


class _Analyzer(ast.NodeVisitor):
    def __init__(self, relpath: str, tree: ast.Module) -> None:
        self.relpath = relpath
        self.fastapi_imported = _module_imports_fastapi(tree)
        self.findings: List[Finding] = []
        imports, bound = _collect_scope(tree.body)
        self.scopes: List[_Scope] = [_Scope("<module>", False, imports, bound)]

    # -- scope bookkeeping -------------------------------------------------- #

    @property
    def current(self) -> _Scope:
        return self.scopes[-1]

    def _qualname(self) -> Optional[str]:
        names = [s.name for s in self.scopes[1:] if not s.name.startswith("<")]
        return ".".join(names) or None

    def _resolve(self, parts: Sequence[str]) -> Optional[str]:
        root = parts[0]
        for scope in reversed(self.scopes):
            if root in scope.bound:
                return None  # shadowed by a parameter, assignment or local def
            if root in scope.imports:
                target = scope.imports[root]
                if target is None:
                    return None  # ambiguous import
                return ".".join([target, *parts[1:]])
        return None  # builtin, star import or undefined: not tracked

    def _add(
        self,
        rule: str,
        node: ast.AST,
        message: str,
        *,
        confidence: str,
        symbol: Optional[str] = None,
    ) -> None:
        self.findings.append(
            Finding(
                rule=rule,
                severity=RULES[rule]["severity"],
                confidence=confidence,
                file=self.relpath,
                line=getattr(node, "lineno", 1),
                column=getattr(node, "col_offset", 0),
                message=message,
                symbol=symbol,
                function=self._qualname(),
            )
        )

    # -- visitors ----------------------------------------------------------- #

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node, is_async=True)

    def _visit_function(self, node: ast.AST, *, is_async: bool) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        # Decorators and defaults are evaluated in the enclosing scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        imports, bound = _collect_scope(node.body, node.args)
        self.scopes.append(_Scope(node.name, is_async, imports, bound))
        self._check_route(node)
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        _, bound = _collect_scope([], node.args)
        self.scopes.append(_Scope("<lambda>", False, {}, bound))
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in [*node.bases, *[k.value for k in node.keywords]]:
            self.visit(base)
        imports, bound = _collect_scope(node.body)
        self.scopes.append(_Scope(node.name, False, imports, bound))
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self.current.is_async:
            self._check_blocking(node)
            self._check_nested_run(node)
        self.generic_visit(node)

    # -- rules -------------------------------------------------------------- #

    def _check_blocking(self, node: ast.Call) -> None:
        parts = _dotted_name(node.func)
        if not parts:
            return
        resolved = self._resolve(parts)
        if resolved is None:
            return
        confidence = BLOCKING_CALLS.get(resolved)
        if confidence is None:
            return
        self._add(
            "ASYNC_BLOCKING_CALL",
            node,
            (
                f"'{resolved}' is called directly inside async function "
                f"'{self._qualname()}'. It blocks the event loop while it runs. "
                "If intentional, offload it (anyio.to_thread.run_sync or "
                "asyncio.to_thread), use an async client, or declare the function "
                "with plain 'def'. Verify before changing."
            ),
            confidence=confidence,
            symbol=resolved,
        )

    def _check_nested_run(self, node: ast.Call) -> None:
        parts = _dotted_name(node.func)
        if parts and self._resolve(parts) == "asyncio.run":
            self._add(
                "ASYNCIO_RUN_NESTED",
                node,
                (
                    f"'asyncio.run' is called inside async function "
                    f"'{self._qualname()}'. asyncio.run cannot be called when an "
                    "event loop is already running; await the coroutine instead."
                ),
                confidence="high",
                symbol="asyncio.run",
            )
            return
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "run_until_complete":
            confidence = "medium"
            receiver = func.value
            if isinstance(receiver, ast.Call):
                inner = _dotted_name(receiver.func)
                if inner and self._resolve(inner) in EVENT_LOOP_FACTORIES:
                    confidence = "high"
            self._add(
                "ASYNCIO_RUN_NESTED",
                node,
                (
                    f"'run_until_complete' is called inside async function "
                    f"'{self._qualname()}'. Driving an event loop from a coroutine "
                    "raises RuntimeError on a running loop; await the coroutine "
                    "instead. Confirm the receiver is an event loop."
                ),
                confidence=confidence,
                symbol="run_until_complete",
            )

    def _check_route(self, node: ast.AST) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if node.returns is not None:
            return
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            parts = _dotted_name(decorator.func)
            if not parts or len(parts) < 2 or parts[-1] not in ROUTE_DECORATOR_METHODS:
                continue
            receiver = parts[-2]
            if not (self.fastapi_imported or "router" in receiver.lower()):
                continue
            keywords = {kw.arg for kw in decorator.keywords if kw.arg}
            if "response_model" in keywords or "response_class" in keywords:
                return  # explicit response contract present
            verb = parts[-1].upper()
            path = ""
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                if isinstance(decorator.args[0].value, str):
                    path = " " + decorator.args[0].value
            self._add(
                "ROUTE_NO_RETURN_ANNOTATION",
                node,
                (
                    f"Route '{node.name}' ({verb}{path}) has no return annotation and "
                    "no response_model/response_class. FastAPI will serialize whatever "
                    "is returned without validation or field filtering. Add a return "
                    "annotation (-> XRead) or response_model if a response schema is "
                    "intended."
                ),
                confidence="high" if self.fastapi_imported else "medium",
                symbol=f"{'.'.join(parts[:-1])}.{parts[-1]}",
            )
            return


# --------------------------------------------------------------------------- #
# File discovery and project audit
# --------------------------------------------------------------------------- #


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


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
) -> Tuple[List[Tuple[str, str]], List[Dict[str, str]]]:
    """Return ``([(relpath, abspath), ...], [skipped, ...])`` deterministically.

    Symlinked directories are never followed. Symlinked files are analysed
    only when they resolve inside ``root``; a file reachable through several
    paths is analysed once, preferring the non-symlink path. With
    ``exclude_tests`` test directories and test-named files are not analysed;
    the number left out is appended to ``tests_excluded`` when a list is given.
    """
    excluded = set(DEFAULT_EXCLUDED_DIRS) | set(excluded_dirs)
    root_abs = os.path.abspath(root)
    root_real = os.path.realpath(root_abs)
    candidates: List[Tuple[bool, str, str, str]] = []  # (is_link, rel, abs, real)
    skipped: List[Dict[str, str]] = []
    left_out = 0

    if os.path.isfile(root_abs):
        rel = os.path.basename(root_abs)
        if exclude_tests and _is_test_path(rel):
            if tests_excluded is not None:
                tests_excluded.append(1)
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


def audit_source(
    source: bytes, relpath: str, *, honor_suppressions: bool = True
) -> List[Finding]:
    """Analyse one Python source buffer. Never raises for malformed input.

    Findings in test code keep their severity but ``ASYNC_BLOCKING_CALL`` drops
    one confidence level (the stalled loop is the test's own). Findings on a
    line carrying a documented suppression are kept and marked ``suppressed``.
    """
    environment = classify_environment(relpath)
    try:
        tree = ast.parse(source, filename=relpath)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None) or 1
        column = getattr(exc, "offset", None) or 0
        detail = getattr(exc, "msg", None) or exc.__class__.__name__
        return [
            Finding(
                rule="PY_SYNTAX",
                severity=RULES["PY_SYNTAX"]["severity"],
                confidence="high",
                file=relpath,
                line=int(line),
                column=int(column),
                message=(
                    f"File could not be parsed with Python {PYTHON_VERSION}: {detail}. "
                    "Other rules did not run on it. If the project targets a newer "
                    "Python, rerun the auditor with that interpreter."
                ),
                environment=environment,
            )
        ]
    except (RecursionError, MemoryError):
        return [
            Finding(
                rule="FILE_SKIPPED",
                severity=RULES["FILE_SKIPPED"]["severity"],
                confidence="high",
                file=relpath,
                line=1,
                column=0,
                message="File exceeded parser limits (nesting depth or memory) and was skipped.",
                environment=environment,
            )
        ]
    analyzer = _Analyzer(relpath, tree)
    analyzer.visit(tree)
    comments = _line_comments(source) if (honor_suppressions and analyzer.findings) else {}
    findings: List[Finding] = []
    for finding in analyzer.findings:
        confidence = finding.confidence
        message = finding.message
        if environment == ENV_TEST and finding.rule == "ASYNC_BLOCKING_CALL":
            confidence = CONFIDENCE_DOWNGRADE[confidence]
            message += (
                " Test code: the stalled event loop is the test's own; confirm the call "
                "does not run inside the system under test."
            )
        suppression = _suppression_for(finding.rule, comments.get(finding.line))
        findings.append(
            replace(
                finding,
                confidence=confidence,
                message=message,
                environment=environment,
                suppressed=suppression is not None,
                suppression=suppression,
            )
        )
    return findings


def audit_project(
    root: str,
    *,
    excluded_dirs: Iterable[str] = (),
    include_hidden: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    exclude_tests: bool = False,
    honor_suppressions: bool = True,
) -> Dict[str, object]:
    """Audit a directory (or single file) and return the JSON-ready report."""
    root_abs = os.path.abspath(root)
    if not os.path.exists(root_abs):
        raise FileNotFoundError(root_abs)

    tests_excluded: List[int] = []
    files, skipped = iter_python_files(
        root_abs,
        excluded_dirs=excluded_dirs,
        include_hidden=include_hidden,
        exclude_tests=exclude_tests,
        tests_excluded=tests_excluded,
    )
    findings: List[Finding] = []
    for entry in skipped:
        if entry["reason"] == "unreadable_directory":
            findings.append(
                Finding(
                    "FILE_SKIPPED",
                    RULES["FILE_SKIPPED"]["severity"],
                    "high",
                    entry["file"],
                    1,
                    0,
                    "Directory could not be listed and was skipped; the analysis of "
                    "this subtree is partial.",
                    environment=classify_environment(entry["file"]),
                )
            )
    files_scanned = 0
    for rel, full in files:
        try:
            size = os.stat(full).st_size
            if size > max_file_bytes:
                skipped.append({"file": rel, "reason": "exceeds_max_file_bytes"})
                findings.append(
                    Finding(
                        "FILE_SKIPPED",
                        RULES["FILE_SKIPPED"]["severity"],
                        "high",
                        rel,
                        1,
                        0,
                        f"File is larger than {max_file_bytes} bytes and was skipped.",
                        environment=classify_environment(rel),
                    )
                )
                continue
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            skipped.append({"file": rel, "reason": "unreadable"})
            findings.append(
                Finding(
                    "FILE_SKIPPED",
                    RULES["FILE_SKIPPED"]["severity"],
                    "high",
                    rel,
                    1,
                    0,
                    f"File could not be read ({exc.__class__.__name__}) and was skipped.",
                    environment=classify_environment(rel),
                )
            )
            continue
        files_scanned += 1
        findings.extend(audit_source(data, rel, honor_suppressions=honor_suppressions))

    findings.sort(key=Finding.sort_key)
    skipped.sort(key=lambda item: (item["file"], item["reason"]))

    by_rule: Dict[str, int] = {}
    by_severity: Dict[str, int] = {level: 0 for level in SEVERITY_ORDER}
    by_environment: Dict[str, int] = {ENV_APPLICATION: 0, ENV_TEST: 0}
    suppressed = 0
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
        by_severity[finding.severity] += 1
        by_environment[finding.environment] += 1
        suppressed += int(finding.suppressed)

    partial = bool(skipped) or "PY_SYNTAX" in by_rule
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION, "python": PYTHON_VERSION},
        "project_root": root_abs,
        "files_scanned": files_scanned,
        "files_skipped": skipped,
        "tests_excluded": sum(tests_excluded),
        "summary": {
            "findings_total": len(findings),
            "by_rule": dict(sorted(by_rule.items())),
            "by_severity": by_severity,
            "by_environment": by_environment,
            "suppressed": suppressed,
            "partial_analysis": partial,
        },
        "findings": [finding.to_dict() for finding in findings],
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# Rendering and CLI
# --------------------------------------------------------------------------- #


def to_json(report: Dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_text(report: Dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    sev = summary["by_severity"]
    tool = report["tool"]
    assert isinstance(tool, dict)
    lines = [
        (
            f"FastAPI static audit ({TOOL_NAME} {TOOL_VERSION}, schema {SCHEMA_VERSION}, "
            f"python {tool['python']})"
        ),
        f"root: {report['project_root']}",
        (
            f"files scanned: {report['files_scanned']}  "
            f"skipped: {len(report['files_skipped'])}  "  # type: ignore[arg-type]
            f"findings: {summary['findings_total']} "
            f"(high {sev['high']}, medium {sev['medium']}, low {sev['low']}, info {sev['info']})"
            f"  partial analysis: {'yes' if summary['partial_analysis'] else 'no'}"
        ),
        (
            f"environment: application {summary['by_environment'][ENV_APPLICATION]}, "
            f"test {summary['by_environment'][ENV_TEST]}  "
            f"suppressed: {summary['suppressed']}  "
            f"tests excluded: {report['tests_excluded']}"
        ),
    ]
    findings = report["findings"]
    assert isinstance(findings, list)
    active = [item for item in findings if not item["suppressed"]]
    suppressed = [item for item in findings if item["suppressed"]]
    for title, items in (("", active), ("suppressed by documented markers (kept for review):", suppressed)):
        if not items:
            continue
        lines.append("")
        if title:
            lines.append(title)
        for item in items:
            lines.append(
                f"{item['file']}:{item['line']}:{item['column']}  {item['rule']}  "
                f"[{item['severity']}/{item['confidence']}/{item['environment']}]  "
                f"{item['message']}"
                + (f"  (suppressed: {item['suppression']})" if item["suppressed"] else "")
            )
    lines.append("")
    lines.append("disclaimer: " + str(report["disclaimer"]))
    return "\n".join(lines) + "\n"


def _list_rules() -> str:
    lines = []
    for rule, meta in sorted(RULES.items()):
        lines.append(f"{rule}  [{meta['severity']}]  {meta['summary']}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Read-only static audit of a FastAPI project. Findings are heuristic "
            "review signals, not proven defects."
        ),
    )
    parser.add_argument("path", nargs="?", help="project directory or single .py file")
    parser.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="write the JSON report to FILE (the only write this tool performs)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="DIRNAME",
        help="additional directory name to skip (repeatable)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="also scan dot-directories (the default exclusion list still applies)",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=f"skip files larger than this many bytes (default {DEFAULT_MAX_FILE_BYTES})",
    )
    parser.add_argument(
        "--exclude-tests",
        action="store_true",
        help=(
            "do not analyse test directories (tests/, test/, testing/, __tests__/) or "
            "test-named files (test_*.py, *_test.py, conftest.py); off by default, test "
            "findings are otherwise reported with environment=test"
        ),
    )
    parser.add_argument(
        "--no-suppressions",
        action="store_true",
        help=(
            "ignore documented suppressions ('# noqa: ASYNC2xx', '# audit-fastapi: ignore') "
            "and report every finding as active"
        ),
    )
    parser.add_argument(
        "--fail-on",
        choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get),
        help=(
            "exit with status 1 if any non-suppressed finding has this severity or higher"
        ),
    )
    parser.add_argument("--list-rules", action="store_true", help="list rules and exit")
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_rules:
        sys.stdout.write(_list_rules())
        return 0
    if not args.path:
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{TOOL_NAME}: error: a project path is required\n")
        return 2
    try:
        report = audit_project(
            args.path,
            excluded_dirs=args.exclude,
            include_hidden=args.include_hidden,
            max_file_bytes=args.max_file_bytes,
            exclude_tests=args.exclude_tests,
            honor_suppressions=not args.no_suppressions,
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"{TOOL_NAME}: error: path does not exist: {exc}\n")
        return 2

    payload = to_json(report)
    output_error = None
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            output_error = f"{TOOL_NAME}: error: cannot write report to {args.output}: {reason}"
    if args.json:
        sys.stdout.write(payload)
    elif not args.output:
        sys.stdout.write(render_text(report))
    elif output_error is None:
        sys.stdout.write(f"report written to {args.output}\n")
    if output_error is not None:
        sys.stderr.write(output_error + "\n")
        return 2

    if args.fail_on:
        threshold = SEVERITY_ORDER[args.fail_on]
        findings = report["findings"]
        assert isinstance(findings, list)
        if any(
            SEVERITY_ORDER[f["severity"]] >= threshold and not f["suppressed"] for f in findings
        ):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
