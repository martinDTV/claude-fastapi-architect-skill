"""Shared helpers for the auditor test modules (unittest and pytest)."""

from __future__ import annotations

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
SKILL_SCRIPTS = REPO_ROOT / "plugins" / "fastapi-architect" / "skills" / "fastapi-architect" / "scripts"
SKILL_SCRIPT = SKILL_SCRIPTS / "audit_fastapi.py"
ROOT_SHIM = REPO_ROOT / "scripts" / "audit_fastapi.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def load_auditor():
    """Import the entry module (which re-exports the fastapi_audit package) without bytecode."""
    sys.dont_write_bytecode = True
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    spec = importlib.util.spec_from_file_location("audit_fastapi_under_test", SKILL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = load_auditor()


class ProjectCase(unittest.TestCase):
    """A fresh temporary project per test, plus helpers to audit it."""

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
        items = self.run_audit(**kwargs)["findings"]
        assert isinstance(items, list)
        return [f for f in items if rule is None or f["rule"] == rule]

    def rules(self, **kwargs) -> List[str]:
        return sorted(f["rule"] for f in self.findings(**kwargs))

    def cli(self, *args: str, script: Path = ROOT_SHIM, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(script), *args],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            timeout=120,
            input=stdin,
        )


def audit_fixture(name: str, **kwargs) -> Dict[str, object]:
    """Audit a committed fixture project under tests/fixtures/<name>."""
    path = FIXTURES / name
    assert path.is_dir(), f"missing fixture {path}"
    return audit.audit_project(str(path), **kwargs)


def by_rule(report: Dict[str, object]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for finding in report["findings"]:  # type: ignore[index]
        grouped.setdefault(finding["rule"], []).append(finding)
    return grouped


def lines_of(report: Dict[str, object], rule: str, file: Optional[str] = None) -> List[int]:
    return sorted(
        f["line"] for f in report["findings"]  # type: ignore[index]
        if f["rule"] == rule and (file is None or f["file"] == file)
    )


def dump(report: Dict[str, object]) -> str:  # debugging aid
    return json.dumps(report["summary"], indent=1) + "\n" + "\n".join(
        f"{f['file']}:{f['line']} {f['rule']} [{f['severity']}/{f['confidence']}/{f['environment']}] {f['message'][:120]}"
        for f in report["findings"]  # type: ignore[index]
    )
