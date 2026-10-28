"""v1.4 CLI and report contract: formats, filters, baseline comparison, changed files, config,
schema 1.2 additivity, determinism and read-only behaviour on the committed fixtures."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, ProjectCase, audit, audit_fixture


def _snapshot(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class FormatsAndFiltersTests(ProjectCase):
    def _populate(self) -> None:
        self.write("app/svc.py", """
            import time
            from sqlalchemy.orm import Session, sessionmaker

            Local = sessionmaker()
            db = Local()

            async def h(session: Session):
                time.sleep(1)
                session.execute("select 1")
            """)

    def test_markdown_and_json_formats(self) -> None:
        self._populate()
        md = self.cli(str(self.root), "--format", "markdown")
        self.assertEqual(md.returncode, 0, md.stderr)
        self.assertIn("# FastAPI static audit", md.stdout)
        self.assertIn("## Findings", md.stdout)
        self.assertIn("SA_SESSION_MODULE_GLOBAL", md.stdout)
        self.assertIn("## Project facts", md.stdout)
        js = self.cli(str(self.root), "--format", "json")
        report = json.loads(js.stdout)
        self.assertEqual(report["schema_version"], audit.SCHEMA_VERSION)
        legacy = self.cli(str(self.root), "--json")
        self.assertEqual(json.loads(legacy.stdout)["summary"], report["summary"])

    def test_category_select_and_ignore(self) -> None:
        self._populate()
        only_async = json.loads(self.cli(str(self.root), "--json", "--category", "async").stdout)
        self.assertEqual(sorted({f["rule"] for f in only_async["findings"]}), ["ASYNC_BLOCKING_CALL"])
        two = json.loads(self.cli(str(self.root), "--json", "--category", "async,sqlalchemy").stdout)
        self.assertEqual(sorted({f["rule"] for f in two["findings"]}), ["ASYNC_BLOCKING_CALL", "SA_SESSION_MODULE_GLOBAL", "SA_SYNC_SESSION_IN_ASYNC_DEF"])
        selected = json.loads(self.cli(str(self.root), "--json", "--select", "SA_SESSION_MODULE_GLOBAL").stdout)
        self.assertEqual([f["rule"] for f in selected["findings"]], ["SA_SESSION_MODULE_GLOBAL"])
        ignored = json.loads(self.cli(str(self.root), "--json", "--ignore", "SA_SESSION_MODULE_GLOBAL,ASYNC_BLOCKING_CALL").stdout)
        self.assertEqual([f["rule"] for f in ignored["findings"]], ["SA_SYNC_SESSION_IN_ASYNC_DEF"])
        self.assertEqual(self.cli(str(self.root), "--category", "nope").returncode, 2)
        self.assertEqual(self.cli(str(self.root), "--select", "NOT_A_RULE").returncode, 2)

    def test_file_level_problems_are_reported_regardless_of_category(self) -> None:
        self._populate()
        self.write("app/broken.py", "def (:\n")
        report = json.loads(self.cli(str(self.root), "--json", "--category", "migrations").stdout)
        self.assertEqual([f["rule"] for f in report["findings"]], ["PY_SYNTAX"])

    def test_include_and_exclude_path_globs(self) -> None:
        self._populate()
        self.write("legacy/old.py", "import time\nasync def h():\n    time.sleep(1)\n")
        excluded = json.loads(self.cli(str(self.root), "--json", "--exclude-path", "legacy/*").stdout)
        self.assertEqual({f["file"] for f in excluded["findings"]}, {"app/svc.py"})
        included = json.loads(self.cli(str(self.root), "--json", "--include-path", "legacy/*").stdout)
        self.assertEqual({f["file"] for f in included["findings"]}, {"legacy/old.py"})
        self.assertEqual(included["files_scanned"], 1)

    def test_changed_files_limits_the_report_but_keeps_project_context(self) -> None:
        self._populate()
        self.write("app/other.py", "import time\nasync def g():\n    time.sleep(2)\n")
        listing = self.root / "changed.txt"
        listing.write_text("app/other.py\n# comment\n", encoding="utf-8")
        report = json.loads(self.cli(str(self.root), "--json", "--changed-files", str(listing)).stdout)
        self.assertEqual({f["file"] for f in report["findings"]}, {"app/other.py"})
        self.assertEqual((report["files_scanned"], report["files_reported"]), (2, 1))
        self.assertTrue(report["config"]["changed_files_only"])
        via_stdin = json.loads(self.cli(str(self.root), "--json", "--changed-files", "-", stdin="app/svc.py\n").stdout)
        self.assertEqual({f["file"] for f in via_stdin["findings"]}, {"app/svc.py"})

    def test_baseline_comparison_and_fail_on_new(self) -> None:
        self._populate()
        baseline_path = self.root / "baseline.json"
        first = self.cli(str(self.root), "--output", str(baseline_path))
        self.assertEqual(first.returncode, 0, first.stderr)
        same = json.loads(self.cli(str(self.root), "--json", "--baseline", str(baseline_path)).stdout)
        self.assertEqual(same["comparison"]["counts"], {"new": 0, "fixed": 0, "unchanged": 3})
        self.assertEqual(self.cli(str(self.root), "--baseline", str(baseline_path), "--fail-on-new").returncode, 0)
        # a new finding appears; an old one disappears
        self.write("app/svc.py", """
            import time
            from sqlalchemy.orm import Session, sessionmaker

            Local = sessionmaker()
            db = Local()

            async def h(session: Session):
                session.execute("select 1")

            async def extra():
                time.sleep(5)
            """)
        changed = json.loads(self.cli(str(self.root), "--json", "--baseline", str(baseline_path)).stdout)
        counts = changed["comparison"]["counts"]
        self.assertEqual((counts["new"], counts["fixed"], counts["unchanged"]), (1, 1, 2))
        self.assertEqual(changed["comparison"]["new"][0]["rule"], "ASYNC_BLOCKING_CALL")
        self.assertEqual(self.cli(str(self.root), "--baseline", str(baseline_path), "--fail-on-new").returncode, 1)
        self.assertEqual(self.cli(str(self.root), "--baseline", str(baseline_path), "--fail-on-new", "--fail-on", "high").returncode, 0)
        self.assertEqual(self.cli(str(self.root), "--baseline", str(baseline_path), "--fail-on-new", "--exit-zero").returncode, 0)
        self.assertEqual(self.cli(str(self.root), "--baseline", str(self.root / "missing.json")).returncode, 2)

    def test_baseline_from_schema_1_1_report_without_fingerprints(self) -> None:
        self._populate()
        report = json.loads(self.cli(str(self.root), "--json").stdout)
        legacy = {"schema_version": "1.1", "findings": [{k: v for k, v in f.items() if k not in {"fingerprint", "category", "verification", "recommended_test"}} for f in report["findings"]]}
        path = self.root / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        compared = json.loads(self.cli(str(self.root), "--json", "--baseline", str(path)).stdout)
        self.assertEqual(compared["comparison"]["counts"]["unchanged"], 3)
        self.assertEqual(compared["comparison"]["baseline_schema"], "1.1")

    def test_config_file_is_read_and_reported(self) -> None:
        self._populate()
        self.write("legacy/old.py", "import time\nasync def h():\n    time.sleep(1)\n")
        (self.root / ".audit-fastapi.json").write_text(json.dumps({"exclude_paths": ["legacy/*"], "ignore_rules": ["SA_SESSION_MODULE_GLOBAL"]}), encoding="utf-8")
        report = json.loads(self.cli(str(self.root), "--json").stdout)
        self.assertEqual(report["facts"]["config_source"], ".audit-fastapi.json")
        self.assertEqual(sorted({f["rule"] for f in report["findings"]}), ["ASYNC_BLOCKING_CALL", "SA_SYNC_SESSION_IN_ASYNC_DEF"])
        self.assertEqual({f["file"] for f in report["findings"]}, {"app/svc.py"})
        (self.root / "bad.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.cli(str(self.root), "--config", str(self.root / "bad.json")).returncode, 2)
        self.assertEqual(self.cli(str(self.root), "--config", str(self.root / "nope.json")).returncode, 2)

    def test_list_rules_covers_every_rule_and_category(self) -> None:
        out = self.cli("--list-rules").stdout
        for rule, meta in audit.RULES.items():
            self.assertIn(rule, out)
            self.assertIn(f"/{meta['category']}/", out)


class ContractTests(ProjectCase):
    def test_schema_1_2_is_a_superset_of_1_0_and_1_1(self) -> None:
        self.write("app/x.py", "import time\nasync def h():\n    time.sleep(1)  # noqa: ASYNC251\n")
        report = self.run_audit()
        top_1_0 = {"schema_version", "tool", "project_root", "files_scanned", "files_skipped", "summary", "findings", "disclaimer"}
        top_1_1 = top_1_0 | {"tests_excluded"}
        top_1_2 = top_1_1 | {"files_reported", "config", "facts", "comparison"}
        self.assertTrue(top_1_2 <= set(report))
        summary_1_1 = {"findings_total", "by_rule", "by_severity", "by_environment", "suppressed", "partial_analysis"}
        self.assertTrue(summary_1_1 | {"by_category", "by_verification"} <= set(report["summary"]))
        finding_1_1 = {"rule", "severity", "confidence", "file", "line", "column", "message", "symbol", "function", "environment", "suppressed", "suppression"}
        finding_1_2 = finding_1_1 | {"category", "verification", "recommended_test", "fingerprint"}
        for finding in report["findings"]:
            self.assertTrue(finding_1_2 <= set(finding))
        # 1.0/1.1 semantics kept: counts include suppressed findings, by_severity has the four levels
        self.assertEqual(report["summary"]["findings_total"], 1)
        self.assertEqual(report["summary"]["suppressed"], 1)
        self.assertEqual(set(report["summary"]["by_severity"]), {"info", "low", "medium", "high"})
        self.assertEqual(report["tool"]["python"], audit.PYTHON_VERSION)
        self.assertIsNone(report["comparison"])

    def test_fixture_reports_are_deterministic_and_read_only(self) -> None:
        for name in ("sa_sessions", "transactions", "alembic_project", "arch_modular", "tenancy_unprotected"):
            path = FIXTURES / name
            before = _snapshot(path)
            first = audit.to_json(audit_fixture(name))
            second = audit.to_json(audit_fixture(name))
            self.assertEqual(first, second, name)
            self.assertEqual(_snapshot(path), before, name)
            self.assertFalse(any(p.name == "__pycache__" for p in path.rglob("*")), name)

    def test_modern_syntax_fixture_depends_on_the_interpreter(self) -> None:
        report = audit_fixture("py313_syntax")
        rules = sorted(f["rule"] for f in report["findings"])
        if sys.version_info >= (3, 12):
            self.assertEqual(rules, ["ASYNC_BLOCKING_CALL"])
            self.assertFalse(report["summary"]["partial_analysis"])
        else:
            self.assertEqual(rules, ["ASYNC_BLOCKING_CALL", "PY_SYNTAX"])
            self.assertTrue(report["summary"]["partial_analysis"])
            self.assertIn(audit.PYTHON_VERSION, [f["message"] for f in report["findings"] if f["rule"] == "PY_SYNTAX"][0])

    def test_facts_block_explains_inference(self) -> None:
        report = audit_fixture("arch_modular")
        facts = report["facts"]
        for key in ("uses_fastapi", "uses_sqlalchemy", "uses_async_sqlalchemy", "uses_alembic", "session_factories", "session_aliases", "protected_routers", "global_auth_signal", "import_linter", "modules_root", "layers", "config_source", "parse_failures"):
            self.assertIn(key, facts)
        self.assertEqual(facts["layers"]["counts"].get("domain"), 2)

    def test_large_project_performance_smoke(self) -> None:
        # 400 small modules with imports between them: must stay well under a few seconds
        import time as _time

        for i in range(400):
            self.write(f"pkg/m{i}.py", f"from pkg.m{(i + 1) % 400} import X\nX = {i}\n")
        start = _time.monotonic()
        report = self.run_audit()
        elapsed = _time.monotonic() - start
        self.assertEqual(report["files_scanned"], 400)
        self.assertEqual([f["rule"] for f in report["findings"]], ["ARCH_IMPORT_CYCLE"])
        self.assertLess(elapsed, 20.0)

    def test_root_shim_and_skill_entry_agree(self) -> None:
        self.write("app/x.py", "import time\nasync def h():\n    time.sleep(1)\n")
        from _audit_support import SKILL_SCRIPT

        a = self.cli(str(self.root), "--json").stdout
        b = self.cli(str(self.root), "--json", script=SKILL_SCRIPT).stdout
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
