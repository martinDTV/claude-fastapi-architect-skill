"""Tests for the FastAPI static auditor (scripts/audit_fastapi.py).

Run with either of:

    python3 -m unittest discover -s tests -v
    python3 -m pytest tests -q

The tests build throwaway projects in temporary directories. They never touch
real projects, the network or external services.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "fastapi-architect"
    / "skills"
    / "fastapi-architect"
    / "scripts"
    / "audit_fastapi.py"
)
ROOT_SHIM = REPO_ROOT / "scripts" / "audit_fastapi.py"


def _load_auditor():
    sys.dont_write_bytecode = True  # keep __pycache__ out of the skill directory
    spec = importlib.util.spec_from_file_location("audit_fastapi_under_test", SKILL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


audit = _load_auditor()


def _symlinks_supported(directory: Path) -> bool:
    target = directory / "_probe_target"
    link = directory / "_probe_link"
    try:
        target.write_text("", encoding="utf-8")
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        for path in (link, target):
            try:
                path.unlink()
            except OSError:
                pass


class ProjectCase(unittest.TestCase):
    """Base class that provides a fresh temporary project per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-audit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def run_audit(self, **kwargs) -> Dict[str, object]:
        return audit.audit_project(str(self.root), **kwargs)

    def findings(self, rule: Optional[str] = None, **kwargs) -> List[Dict[str, object]]:
        report = self.run_audit(**kwargs)
        items = report["findings"]
        assert isinstance(items, list)
        if rule is None:
            return items
        return [f for f in items if f["rule"] == rule]


# --------------------------------------------------------------------------- #
# ASYNC_BLOCKING_CALL
# --------------------------------------------------------------------------- #


class AsyncBlockingCallTests(ProjectCase):
    def test_time_sleep_inside_async_is_flagged(self) -> None:
        self.write(
            "app/handlers.py",
            """
            import time

            async def handler():
                time.sleep(1)
            """,
        )
        found = self.findings("ASYNC_BLOCKING_CALL")
        self.assertEqual(len(found), 1)
        finding = found[0]
        self.assertEqual(finding["file"], "app/handlers.py")
        self.assertEqual(finding["line"], 4)
        self.assertEqual(finding["symbol"], "time.sleep")
        self.assertEqual(finding["function"], "handler")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["confidence"], "high")

    def test_time_sleep_inside_plain_def_is_not_flagged(self) -> None:
        self.write(
            "app/sync.py",
            """
            import time

            def handler():
                time.sleep(1)

            class Service:
                def run(self):
                    time.sleep(2)
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])

    def test_await_asyncio_sleep_is_not_flagged(self) -> None:
        self.write(
            "app/ok.py",
            """
            import asyncio
            from asyncio import sleep

            async def handler():
                await asyncio.sleep(1)
                await sleep(0.5)
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])

    def test_import_aliases_are_resolved(self) -> None:
        self.write(
            "app/aliases.py",
            """
            import time as t
            from time import sleep as pause
            import requests as http
            from urllib import request
            import urllib.request
            import subprocess as sp

            async def handler():
                t.sleep(1)
                pause(1)
                http.post("https://example.invalid")
                request.urlopen("https://example.invalid")
                urllib.request.urlopen("https://example.invalid")
                sp.check_output(["ls"])
            """,
        )
        symbols = sorted(f["symbol"] for f in self.findings("ASYNC_BLOCKING_CALL"))
        self.assertEqual(
            symbols,
            [
                "requests.post",
                "subprocess.check_output",
                "time.sleep",
                "time.sleep",
                "urllib.request.urlopen",
                "urllib.request.urlopen",
            ],
        )

    def test_names_from_other_modules_or_redefined_are_not_flagged(self) -> None:
        self.write(
            "app/shadow.py",
            """
            from mylib import sleep
            from .local import get as requests_get
            import requests

            def sleep_wrapper():
                pass

            requests = object()  # rebinds the module name

            async def handler():
                sleep(1)
                requests_get("x")
                requests.get("x")
                sleep_wrapper()
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])

    def test_parameter_and_local_assignment_shadowing(self) -> None:
        self.write(
            "app/params.py",
            """
            import requests
            import time

            async def handler(requests):
                requests.get("x")

            async def other():
                time = object()
                time.sleep(1)

            async def inner_import():
                import time as clock
                clock.sleep(1)
            """,
        )
        found = self.findings("ASYNC_BLOCKING_CALL")
        self.assertEqual([f["function"] for f in found], ["inner_import"])

    def test_ambiguous_import_is_not_flagged(self) -> None:
        self.write(
            "app/ambiguous.py",
            """
            from time import sleep
            from asyncio import sleep

            async def handler():
                sleep(1)
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])

    def test_nested_scopes_use_innermost_function(self) -> None:
        self.write(
            "app/nested.py",
            """
            import time

            async def outer():
                def sync_helper():
                    time.sleep(1)   # not flagged: innermost function is sync
                async def inner():
                    time.sleep(2)   # flagged: innermost function is async
                cb = lambda: time.sleep(3)  # not flagged: lambda body is not run here
                values = [time.sleep(i) for i in range(2)]  # flagged: comprehension runs inline
                return sync_helper, inner, cb, values

            def sync_outer():
                async def coroutine():
                    time.sleep(4)   # flagged
                return coroutine
            """,
        )
        found = self.findings("ASYNC_BLOCKING_CALL")
        self.assertEqual(
            [(f["line"], f["function"]) for f in found],
            [(7, "outer.inner"), (9, "outer"), (14, "sync_outer.coroutine")],
        )

    def test_callable_passed_as_reference_is_not_flagged(self) -> None:
        self.write(
            "app/offload.py",
            """
            import asyncio
            import time
            import requests
            import anyio

            async def handler(url):
                await anyio.to_thread.run_sync(time.sleep, 1)
                await asyncio.to_thread(requests.get, url)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, time.sleep, 1)
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])

    def test_subprocess_run_flagged_but_popen_not(self) -> None:
        self.write(
            "app/proc.py",
            """
            import subprocess

            async def handler():
                subprocess.run(["ls"])
                subprocess.check_output(["ls"])
                proc = subprocess.Popen(["ls"])
                return proc
            """,
        )
        symbols = sorted(f["symbol"] for f in self.findings("ASYNC_BLOCKING_CALL"))
        self.assertEqual(symbols, ["subprocess.check_output", "subprocess.run"])

    def test_instance_method_calls_are_not_tracked(self) -> None:
        # Documented limitation: calls through attributes or instances are not resolved.
        self.write(
            "app/instances.py",
            """
            import requests

            class Client:
                def __init__(self):
                    self.http = requests.Session()

                async def fetch(self):
                    return self.http.get("x")
            """,
        )
        self.assertEqual(self.findings("ASYNC_BLOCKING_CALL"), [])


# --------------------------------------------------------------------------- #
# ASYNCIO_RUN_NESTED
# --------------------------------------------------------------------------- #


class AsyncioRunNestedTests(ProjectCase):
    def test_asyncio_run_inside_async_is_flagged(self) -> None:
        self.write(
            "app/runner.py",
            """
            import asyncio

            async def work():
                return 1

            async def handler():
                return asyncio.run(work())

            def main():
                return asyncio.run(work())  # legitimate entry point
            """,
        )
        found = self.findings("ASYNCIO_RUN_NESTED")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["function"], "handler")
        self.assertEqual(found[0]["severity"], "high")
        self.assertEqual(found[0]["confidence"], "high")

    def test_run_until_complete_inside_async(self) -> None:
        self.write(
            "app/loops.py",
            """
            import asyncio

            async def work():
                return 1

            async def handler(loop):
                asyncio.get_event_loop().run_until_complete(work())
                loop.run_until_complete(work())
            """,
        )
        found = self.findings("ASYNCIO_RUN_NESTED")
        self.assertEqual([f["confidence"] for f in found], ["high", "medium"])
        self.assertEqual([f["line"] for f in found], [7, 8])


# --------------------------------------------------------------------------- #
# ROUTE_NO_RETURN_ANNOTATION
# --------------------------------------------------------------------------- #


class RouteAnnotationTests(ProjectCase):
    def test_route_without_return_annotation_is_informational(self) -> None:
        self.write(
            "app/routes.py",
            """
            from fastapi import APIRouter
            from app.schemas import ItemRead

            router = APIRouter()

            @router.get("/items")
            async def list_items():
                return []

            @router.post("/items", status_code=201)
            async def create_item(payload: dict) -> ItemRead:
                return ItemRead()

            @router.delete("/items/{item_id}", status_code=204)
            def delete_item(item_id: int) -> None:
                return None
            """,
        )
        found = self.findings("ROUTE_NO_RETURN_ANNOTATION")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["function"], "list_items")
        self.assertEqual(found[0]["severity"], "info")
        self.assertEqual(found[0]["confidence"], "high")
        self.assertIn("GET /items", found[0]["message"])

    def test_response_model_and_response_class_suppress_the_finding(self) -> None:
        self.write(
            "app/routes.py",
            """
            from fastapi import APIRouter
            from fastapi.responses import HTMLResponse
            from app.schemas import ItemRead

            router = APIRouter()

            @router.get("/items", response_model=list[ItemRead])
            async def list_items():
                return []

            @router.get("/raw", response_model=None)
            async def raw():
                return b""

            @router.get("/page", response_class=HTMLResponse)
            async def page():
                return "<html></html>"
            """,
        )
        self.assertEqual(self.findings("ROUTE_NO_RETURN_ANNOTATION"), [])

    def test_routes_inside_app_factory_are_detected(self) -> None:
        self.write(
            "app/main.py",
            """
            from fastapi import FastAPI

            def create_app():
                app = FastAPI()

                @app.get("/health")
                async def health():
                    return {"status": "ok"}

                return app
            """,
        )
        found = self.findings("ROUTE_NO_RETURN_ANNOTATION")
        self.assertEqual([f["function"] for f in found], ["create_app.health"])

    def test_non_fastapi_decorators_are_not_flagged(self) -> None:
        self.write(
            "app/flask_app.py",
            """
            from flask import Flask

            app = Flask(__name__)

            @app.get("/")
            def index():
                return "hi"
            """,
        )
        self.write(
            "app/aiohttp_app.py",
            """
            from aiohttp import web

            routes = web.RouteTableDef()

            @routes.get("/")
            async def index(request):
                return web.Response()
            """,
        )
        self.assertEqual(self.findings("ROUTE_NO_RETURN_ANNOTATION"), [])

    def test_router_receiver_without_fastapi_import_has_medium_confidence(self) -> None:
        self.write(
            "app/orders/router.py",
            """
            from app.core.routing import router

            @router.get("/orders")
            async def list_orders():
                return []
            """,
        )
        found = self.findings("ROUTE_NO_RETURN_ANNOTATION")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "medium")


# --------------------------------------------------------------------------- #
# PY_SYNTAX, unreadable files, size limits
# --------------------------------------------------------------------------- #


class RobustnessTests(ProjectCase):
    def test_syntax_error_is_recorded_without_aborting(self) -> None:
        self.write("app/broken.py", "def oops(:\n    pass\n")
        self.write(
            "app/good.py",
            """
            import time

            async def handler():
                time.sleep(1)
            """,
        )
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 2)
        rules = [(f["file"], f["rule"]) for f in report["findings"]]
        self.assertEqual(
            rules,
            [("app/broken.py", "PY_SYNTAX"), ("app/good.py", "ASYNC_BLOCKING_CALL")],
        )
        syntax = report["findings"][0]
        self.assertEqual(syntax["line"], 1)
        self.assertEqual(syntax["severity"], "low")
        self.assertNotIn("def oops", syntax["message"])  # no source fragments

    def test_null_bytes_and_bad_encoding_do_not_abort(self) -> None:
        (self.root / "nul.py").write_bytes(b"x = 1\x00\n")
        (self.root / "latin.py").write_bytes(b"s = '\xe9'\n")  # invalid UTF-8, no cookie
        (self.root / "cookie.py").write_bytes(b"# -*- coding: latin-1 -*-\ns = '\xe9'\n")
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 3)
        by_file = {f["file"]: f["rule"] for f in report["findings"]}
        self.assertEqual(by_file.get("nul.py"), "PY_SYNTAX")
        self.assertEqual(by_file.get("latin.py"), "PY_SYNTAX")
        self.assertNotIn("cookie.py", by_file)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root can read unreadable files"
    )
    @unittest.skipIf(sys.platform.startswith("win"), "POSIX permissions required")
    def test_unreadable_file_is_reported_and_scan_continues(self) -> None:
        secret = self.write("app/locked.py", "import time\n")
        self.write("app/other.py", "import time\nasync def h():\n    time.sleep(1)\n")
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual(
            report["files_skipped"], [{"file": "app/locked.py", "reason": "unreadable"}]
        )
        rules = [(f["file"], f["rule"]) for f in report["findings"]]
        self.assertIn(("app/locked.py", "FILE_SKIPPED"), rules)
        self.assertIn(("app/other.py", "ASYNC_BLOCKING_CALL"), rules)

    def test_files_above_size_limit_are_skipped(self) -> None:
        self.write("app/big.py", "x = 1\n" * 200)
        self.write("app/small.py", "x = 1\n")
        report = self.run_audit(max_file_bytes=100)
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual(
            report["files_skipped"],
            [{"file": "app/big.py", "reason": "exceeds_max_file_bytes"}],
        )
        self.assertEqual(report["findings"][0]["rule"], "FILE_SKIPPED")

    def test_missing_path_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            audit.audit_project(str(self.root / "does-not-exist"))

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root can list unreadable directories"
    )
    @unittest.skipIf(sys.platform.startswith("win"), "POSIX permissions required")
    def test_unreadable_directory_marks_the_analysis_partial(self) -> None:
        self.write("app/ok.py", "x = 1\n")
        locked = self.write("locked/inside.py", "import time\nasync def h():\n    time.sleep(1)\n")
        locked.parent.chmod(0)
        self.addCleanup(locked.parent.chmod, 0o755)
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual(
            report["files_skipped"], [{"file": "locked", "reason": "unreadable_directory"}]
        )
        self.assertEqual(
            [(f["file"], f["rule"]) for f in report["findings"]], [("locked", "FILE_SKIPPED")]
        )
        self.assertTrue(report["summary"]["partial_analysis"])

    def test_partial_analysis_flag_is_false_for_a_clean_scan(self) -> None:
        self.write("app/ok.py", "x = 1\n")
        report = self.run_audit()
        self.assertFalse(report["summary"]["partial_analysis"])
        self.write("app/broken.py", "def (:\n")
        self.assertTrue(self.run_audit()["summary"]["partial_analysis"])


# --------------------------------------------------------------------------- #
# Traversal: exclusions, hidden directories, symlinks
# --------------------------------------------------------------------------- #


class TraversalTests(ProjectCase):
    BLOCKING = "import time\nasync def h():\n    time.sleep(1)\n"

    def test_excluded_and_hidden_directories_are_skipped(self) -> None:
        for folder in [
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            "dist",
            "build",
            ".hidden",
            "src/pkg/.cache",
        ]:
            self.write(f"{folder}/ignored.py", self.BLOCKING)
        self.write("src/pkg/handlers.py", self.BLOCKING)
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual([f["file"] for f in report["findings"]], ["src/pkg/handlers.py"])

    def test_include_hidden_scans_dot_dirs_but_not_default_exclusions(self) -> None:
        self.write(".hidden/tool.py", self.BLOCKING)
        self.write(".venv/lib/site.py", self.BLOCKING)
        report = self.run_audit(include_hidden=True)
        self.assertEqual([f["file"] for f in report["findings"]], [".hidden/tool.py"])

    def test_extra_exclusions_are_honoured(self) -> None:
        self.write("generated/code.py", self.BLOCKING)
        self.write("src/code.py", self.BLOCKING)
        report = self.run_audit(excluded_dirs=["generated"])
        self.assertEqual([f["file"] for f in report["findings"]], ["src/code.py"])

    def test_non_python_files_are_ignored(self) -> None:
        self.write("README.md", "import time\n")
        self.write("notes.txt", self.BLOCKING)
        self.write("types.pyi", self.BLOCKING)
        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 0)
        self.assertEqual(report["findings"], [])

    def test_single_file_path_is_supported(self) -> None:
        path = self.write("app/one.py", self.BLOCKING)
        report = audit.audit_project(str(path))
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual([f["file"] for f in report["findings"]], ["one.py"])

    def test_symlinks_do_not_duplicate_or_escape_the_root(self) -> None:
        if not _symlinks_supported(self.root):
            self.skipTest("symlinks not supported on this filesystem")
        real = self.write("src/real.py", self.BLOCKING)
        os.symlink(real, self.root / "src" / "alias.py")  # duplicate of a scanned file
        os.symlink(self.root / "src", self.root / "linked_dir")  # directory symlink
        outside = tempfile.TemporaryDirectory(prefix="fa-outside-")
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name) / "outside.py"
        outside_file.write_text(self.BLOCKING, encoding="utf-8")
        os.symlink(outside_file, self.root / "src" / "escape.py")

        report = self.run_audit()
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual([f["file"] for f in report["findings"]], ["src/real.py"])
        self.assertEqual(
            report["files_skipped"],
            [
                {"file": "src/alias.py", "reason": "duplicate_of_scanned_file"},
                {"file": "src/escape.py", "reason": "symlink_outside_root"},
            ],
        )


# --------------------------------------------------------------------------- #
# Report shape, determinism, read-only behaviour
# --------------------------------------------------------------------------- #


def _snapshot(root: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file():
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            snapshot[rel] = "<dir>"
    return snapshot


class ReportTests(ProjectCase):
    def _populate(self) -> None:
        self.write(
            "app/z_last.py",
            """
            import time
            import asyncio

            async def z():
                time.sleep(1)
                asyncio.run(asyncio.sleep(0))
            """,
        )
        self.write(
            "app/a_first.py",
            """
            from fastapi import APIRouter
            import requests

            router = APIRouter()

            @router.get("/x")
            async def x():
                return requests.get("x")
            """,
        )
        self.write("app/broken.py", "if True\n")

    def test_report_schema(self) -> None:
        self._populate()
        report = self.run_audit()
        for key in (
            "schema_version",
            "tool",
            "project_root",
            "files_scanned",
            "files_skipped",
            "summary",
            "findings",
            "disclaimer",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["schema_version"], audit.SCHEMA_VERSION)
        self.assertEqual(report["project_root"], str(self.root))
        self.assertEqual(report["tool"]["python"], audit.PYTHON_VERSION)
        self.assertIn("partial_analysis", report["summary"])
        self.assertIn("heuristic", report["disclaimer"])
        self.assertIn("not demonstrated defects", report["disclaimer"])
        for finding in report["findings"]:
            for key in ("rule", "severity", "confidence", "file", "line", "message"):
                self.assertIn(key, finding)
            self.assertIn(finding["severity"], audit.SEVERITY_ORDER)
            self.assertIn(finding["confidence"], audit.CONFIDENCE_ORDER)
            self.assertIn(finding["rule"], audit.RULES)
        summary = report["summary"]
        self.assertEqual(summary["findings_total"], len(report["findings"]))
        self.assertEqual(sum(summary["by_rule"].values()), len(report["findings"]))
        self.assertEqual(sum(summary["by_severity"].values()), len(report["findings"]))

    def test_output_is_deterministic_and_sorted(self) -> None:
        self._populate()
        first = audit.to_json(self.run_audit())
        second = audit.to_json(self.run_audit())
        self.assertEqual(first, second)
        json.loads(first)  # valid JSON
        findings = json.loads(first)["findings"]
        keys = [(f["file"], f["line"], f["column"], f["rule"]) for f in findings]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            [f["file"] for f in findings],
            ["app/a_first.py", "app/a_first.py", "app/broken.py", "app/z_last.py", "app/z_last.py"],
        )

    def test_audit_does_not_modify_the_project(self) -> None:
        self._populate()
        before = _snapshot(self.root)
        self.run_audit()
        self.run_audit()
        self.assertEqual(_snapshot(self.root), before)
        self.assertFalse(any("__pycache__" in p for p in _snapshot(self.root)))

    def test_text_rendering_contains_summary_and_findings(self) -> None:
        self._populate()
        text = audit.render_text(self.run_audit())
        self.assertIn("files scanned: 3", text)
        self.assertIn("app/z_last.py:5:4  ASYNC_BLOCKING_CALL", text)
        self.assertIn("disclaimer:", text)

    def test_audit_source_helper(self) -> None:
        found = audit.audit_source(b"import time\nasync def f():\n    time.sleep(1)\n", "x.py")
        self.assertEqual([f.rule for f in found], ["ASYNC_BLOCKING_CALL"])
        self.assertEqual([f.rule for f in audit.audit_source(b"def (:\n", "y.py")], ["PY_SYNTAX"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class CliTests(ProjectCase):
    def cli(self, *args: str, script: Path = ROOT_SHIM) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(script), *args],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            timeout=60,
        )

    def _populate(self) -> None:
        self.write("app/h.py", "import time\nasync def h():\n    time.sleep(1)\n")

    def test_json_flag_prints_valid_report(self) -> None:
        self._populate()
        result = self.cli(str(self.root), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["files_scanned"], 1)
        self.assertEqual(report["findings"][0]["rule"], "ASYNC_BLOCKING_CALL")
        self.assertEqual(report["project_root"], str(self.root))

    def test_output_flag_writes_file_and_text_mode_prints_summary(self) -> None:
        self._populate()
        out = Path(self._tmp.name) / "report.json"
        result = self.cli(str(self.root), "--output", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("report written to", result.stdout)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["files_scanned"], 1)

        text = self.cli(str(self.root))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("FastAPI static audit", text.stdout)
        self.assertIn("ASYNC_BLOCKING_CALL", text.stdout)

    def test_exit_codes(self) -> None:
        self._populate()
        self.assertEqual(self.cli(str(self.root / "missing")).returncode, 2)
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.cli(str(self.root), "--fail-on", "medium").returncode, 1)
        self.assertEqual(self.cli(str(self.root), "--fail-on", "high").returncode, 0)
        self.assertEqual(self.cli("--list-rules").returncode, 0)
        self.assertIn("audit_fastapi 1.3.0", self.cli("--version").stdout)

    def test_unwritable_output_path_fails_cleanly(self) -> None:
        self._populate()
        result = self.cli(str(self.root), "--output", str(self.root / "missing-dir" / "r.json"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot write report", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.root / "missing-dir").exists())

    def test_cli_is_deterministic_across_runs(self) -> None:
        self._populate()
        self.write("app/b.py", "import asyncio\nasync def b():\n    asyncio.run(b())\n")
        first = self.cli(str(self.root), "--json").stdout
        second = self.cli(str(self.root), "--json").stdout
        self.assertEqual(first, second)

    def test_root_shim_matches_skill_script(self) -> None:
        self._populate()
        via_shim = self.cli(str(self.root), "--json").stdout
        direct = self.cli(str(self.root), "--json", script=SKILL_SCRIPT).stdout
        self.assertEqual(via_shim, direct)


if __name__ == "__main__":
    unittest.main()
