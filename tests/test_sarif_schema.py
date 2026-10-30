"""SARIF 2.1.0 interoperability: the auditor's documents validate against the official OASIS schema
(vendored under tests/schemas) when `jsonschema` is available, and always satisfy the structural
checks of scripts/validate_sarif.py: relative locations, rule ids, levels, fingerprints, suppressions,
partial results, evidence properties, baseline state, determinism and correspondence with the JSON report."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from _audit_support import REPO_ROOT, ProjectCase

try:  # the schema step is optional; the structural step is not
    import jsonschema  # type: ignore  # noqa: F401

    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - depends on the interpreter
    HAVE_JSONSCHEMA = False


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_sarif_under_test", REPO_ROOT / "scripts" / "validate_sarif.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


class SarifInteroperabilityTests(ProjectCase):
    def _project(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/a.py", '''
            import time
            import requests
            from fastapi import APIRouter

            router = APIRouter()

            async def h():
                time.sleep(1)  # audit-fastapi: ignore[ASYNC_BLOCKING_CALL]
                requests.get("https://x.invalid")

            @router.get("/orders")
            async def orders() -> list:
                return []
        ''')
        self.write("app/broken.py", "def (:\n")  # PY_SYNTAX -> a partial result

    def _documents(self, *extra: str):
        proc = self.cli(".", "--format", "json", "--sarif", "out.sarif", *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        document = json.loads((self.root / "out.sarif").read_text())
        return report, document

    def test_structural_checks_pass_and_match_the_json_report(self) -> None:
        self._project()
        report, document = self._documents()
        self.assertEqual(validator.structural_errors(document, report), [])
        results = document["runs"][0]["results"]
        states = {r["ruleId"]: r["properties"]["evidence_state"] for r in results}
        self.assertEqual(states["PY_SYNTAX"], "partial")
        self.assertTrue(document["runs"][0]["invocations"][0]["properties"]["partial_analysis"])
        self.assertTrue(any(r.get("suppressions") for r in results))
        for r in results:
            self.assertFalse(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"].startswith("/"))

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed in this interpreter")
    def test_documents_validate_against_the_official_schema(self) -> None:
        self._project()
        report, document = self._documents()
        self.assertEqual(validator.schema_errors(document), [])
        # with a baseline: baselineState is emitted and the document stays valid
        (self.root / "base.json").write_text(json.dumps({"schema_version": report["schema_version"], "findings": report["findings"][:1]}))
        _report, with_baseline = self._documents("--baseline", "base.json")
        self.assertEqual(validator.schema_errors(with_baseline), [])
        states = {r["baselineState"] for r in with_baseline["runs"][0]["results"]}
        self.assertEqual(states, {"new", "unchanged"})

    def test_cli_validator_script_and_determinism(self) -> None:
        self._project()
        self._documents()
        first = (self.root / "out.sarif").read_text()
        self._documents()
        self.assertEqual(first, (self.root / "out.sarif").read_text())
        proc = subprocess.run([sys.executable, "-I", str(REPO_ROOT / "scripts" / "validate_sarif.py"), str(self.root / "out.sarif")], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("structure: ok", proc.stdout)

    def test_schema_file_is_the_official_one(self) -> None:
        import hashlib

        path = Path(REPO_ROOT) / "tests" / "schemas" / "sarif-schema-2.1.0.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest()[:16], "c3b4bb2d60938974")
        schema = json.loads(path.read_text())
        self.assertEqual(schema.get("$id") or schema.get("id"), "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json")  # draft-04 uses "id"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
