"""Runner adapters: verifiable evidence that tests were collected, executed and passed (v1.9 acceptance gate).

An exit code is not evidence. Each adapter instruments the declared command so the runner writes a
structured report bound to this run (a nonce), then reads that report back: tests discovered, selected,
executed, passed, failed, errored, skipped, and collection errors. The verdict (``assess``) comes from the
report, never from stdout text or the exit code alone; the exit code only has to be consistent with it.

* ``pytest-junit``: appends ``--junit-xml=<report> -o junit_family=xunit2 -o junit_suite_name=<nonce>`` to any
  command that invokes pytest (``pytest``, ``python -m pytest``, ``uv run ... pytest``). The ``<testsuite>`` must
  carry the nonce; testcases are classified by their child element (``failure``, ``error``, ``skipped``;
  ``error`` with message ``collection failure`` is a collection error). pytest does not report deselected or
  discovered-but-not-selected tests in JUnit, so ``discovered`` is None for this adapter.
* ``unittest-json``: replaces ``-m unittest`` by the tool's own runner (``_unittest_runner.py``, standard
  library only, copied next to the isolated copy), which loads the same tests, records every id and writes a
  JSON report with the nonce.
* anything else: no adapter, outcome ``unverifiable``; a run without verifiable evidence never verifies.

Report integrity checks: the file must exist, be modified after the run started, parse, and carry the nonce.
A missing, corrupt or foreign report is ``no_evidence`` / ``report_corrupt`` / ``report_mismatch``.
"""

from __future__ import annotations

import json
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    OUTCOME_FAILED,
    OUTCOME_INFRASTRUCTURE_ERROR,
    OUTCOME_NO_EVIDENCE,
    OUTCOME_NO_TESTS,
    OUTCOME_NOT_RUN,
    OUTCOME_PASSED,
    OUTCOME_REFUSED,
    OUTCOME_REPORT_CORRUPT,
    OUTCOME_REPORT_MISMATCH,
    OUTCOME_SKIPPED_ONLY,
    OUTCOME_TIMEOUT,
    OUTCOME_UNVERIFIABLE,
    TestRun,
)

COUNT_KEYS = ("discovered", "selected", "executed", "passed", "failed", "errors", "skipped", "collection_errors")
TEST_KEYS = ("passed", "failed", "errors", "skipped", "collection_errors")
MAX_IDS = 200


@dataclass
class Evidence:
    adapter: Optional[str] = None
    report: Optional[str] = None
    nonce: Optional[str] = None
    present: bool = False
    matched: bool = False
    parse_error: Optional[str] = None
    counts: Dict[str, Optional[int]] = field(default_factory=dict)
    tests: Dict[str, List[str]] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    @property
    def verifiable(self) -> bool:
        return bool(self.adapter and self.present and self.matched and not self.parse_error)

    def to_dict(self) -> Dict[str, object]:
        return {
            "adapter": self.adapter,
            "report": self.report,
            "nonce": self.nonce,
            "present": self.present,
            "matched": self.matched,
            "parse_error": self.parse_error,
            "verifiable": self.verifiable,
            "counts": {k: self.counts.get(k) for k in COUNT_KEYS},
            "tests": {k: list(self.tests.get(k, []))[:MAX_IDS] for k in TEST_KEYS},
            "problems": list(self.problems),
        }


class RunnerAdapter:
    name = "abstract"

    def matches(self, argv: Sequence[str]) -> bool:  # pragma: no cover - interface
        return False

    def instrument(self, argv: Sequence[str], evidence_dir: Path, nonce: str) -> Tuple[List[str], Path]:  # pragma: no cover - interface
        raise NotImplementedError

    def collect(self, report: Path, nonce: str, started_at: float) -> Evidence:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def _check_file(report: Path, nonce: str, started_at: float, evidence: Evidence) -> bool:
        evidence.report, evidence.nonce = str(report), nonce
        if not report.is_file():
            evidence.problems.append("the runner wrote no report")
            return False
        evidence.present = True
        try:
            if report.stat().st_mtime < started_at - 1.0:
                evidence.problems.append("the report predates this run")
                return False
        except OSError as exc:
            evidence.problems.append(f"cannot stat the report: {exc}")
            return False
        return True


class PytestJUnitAdapter(RunnerAdapter):
    name = "pytest-junit"

    def matches(self, argv: Sequence[str]) -> bool:
        tokens = list(argv)
        return any(os.path.basename(t) == "pytest" for t in tokens) or any(tokens[i] == "-m" and tokens[i + 1] == "pytest" for i in range(len(tokens) - 1))

    def instrument(self, argv: Sequence[str], evidence_dir: Path, nonce: str) -> Tuple[List[str], Path]:
        report = evidence_dir / f"pytest-{nonce}.xml"
        return [*argv, f"--junit-xml={report}", "-o", "junit_family=xunit2", "-o", f"junit_suite_name={nonce}"], report

    def collect(self, report: Path, nonce: str, started_at: float) -> Evidence:
        evidence = Evidence(adapter=self.name)
        if not self._check_file(report, nonce, started_at, evidence):
            return evidence
        try:
            root = ET.parse(str(report)).getroot()
        except ET.ParseError as exc:
            evidence.parse_error = f"junit report does not parse: {exc}"
            return evidence
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not suites:
            evidence.parse_error = "junit report has no testsuite"
            return evidence
        if any(s.get("name") != nonce for s in suites):
            evidence.problems.append(f"testsuite name {[s.get('name') for s in suites]} does not match the run nonce")
            return evidence
        evidence.matched = True
        tests: Dict[str, List[str]] = {k: [] for k in TEST_KEYS}
        for case in root.iter("testcase"):
            ident = f"{case.get('classname') or ''}::{case.get('name') or ''}".strip(":")
            children = {child.tag: child for child in case}
            if "error" in children and (children["error"].get("message") or "").startswith("collection failure"):
                tests["collection_errors"].append(ident)
            elif "failure" in children:
                tests["failed"].append(ident)
            elif "error" in children:
                tests["errors"].append(ident)
            elif "skipped" in children:
                tests["skipped"].append(ident)
            else:
                tests["passed"].append(ident)
        executed = sum(len(tests[k]) for k in ("passed", "failed", "errors", "skipped"))
        evidence.tests = tests
        evidence.counts = {
            "discovered": None,  # JUnit does not report deselected or uncollected tests
            "selected": executed + len(tests["collection_errors"]),
            "executed": executed,
            "passed": len(tests["passed"]),
            "failed": len(tests["failed"]),
            "errors": len(tests["errors"]),
            "skipped": len(tests["skipped"]),
            "collection_errors": len(tests["collection_errors"]),
        }
        declared = sum(int(s.get("tests") or 0) for s in suites)
        if declared != executed + len(tests["collection_errors"]):
            evidence.problems.append(f"testsuite declares {declared} tests but {executed + len(tests['collection_errors'])} testcases are listed")
        return evidence


class UnittestJsonAdapter(RunnerAdapter):
    name = "unittest-json"
    script = Path(__file__).with_name("_unittest_runner.py")

    def matches(self, argv: Sequence[str]) -> bool:
        tokens = list(argv)
        return any(tokens[i] == "-m" and tokens[i + 1] == "unittest" for i in range(len(tokens) - 1))

    def instrument(self, argv: Sequence[str], evidence_dir: Path, nonce: str) -> Tuple[List[str], Path]:
        tokens = list(argv)
        index = next(i for i in range(len(tokens) - 1) if tokens[i] == "-m" and tokens[i + 1] == "unittest")
        runner = evidence_dir / "fa_unittest_runner.py"
        if not runner.exists():
            shutil.copyfile(self.script, runner)
        report = evidence_dir / f"unittest-{nonce}.json"
        return [*tokens[:index], str(runner), "--output", str(report), "--nonce", nonce, *tokens[index + 2:]], report

    def collect(self, report: Path, nonce: str, started_at: float) -> Evidence:
        evidence = Evidence(adapter=self.name)
        if not self._check_file(report, nonce, started_at, evidence):
            return evidence
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            evidence.parse_error = f"unittest report does not parse: {exc}"
            return evidence
        if not isinstance(data, dict) or not isinstance(data.get("counts"), dict) or not isinstance(data.get("tests"), dict):
            evidence.parse_error = "unittest report has an unexpected shape"
            return evidence
        if data.get("nonce") != nonce:
            evidence.problems.append(f"report nonce {data.get('nonce')!r} does not match the run nonce")
            return evidence
        evidence.matched = True
        evidence.counts = {k: (int(data["counts"][k]) if data["counts"].get(k) is not None else None) for k in COUNT_KEYS}
        evidence.tests = {k: [str(t) for t in data["tests"].get(k, [])] for k in TEST_KEYS}
        return evidence


ADAPTERS: Tuple[RunnerAdapter, ...] = (PytestJUnitAdapter(), UnittestJsonAdapter())


def adapter_for(argv: Sequence[str]) -> Optional[RunnerAdapter]:
    for adapter in ADAPTERS:
        if adapter.matches(argv):
            return adapter
    return None


def assess(run: TestRun, evidence: Optional[Evidence]) -> str:
    """Outcome from the evidence (strict): a run verifies only through a matched, parseable report that shows at
    least one executed, passing test and no failure, error or collection error, with a consistent exit code."""
    if run.refused:
        return OUTCOME_REFUSED
    if not run.ran:
        return OUTCOME_NOT_RUN
    if run.timed_out:
        return OUTCOME_TIMEOUT
    if evidence is None or evidence.adapter is None:
        return OUTCOME_UNVERIFIABLE
    if not evidence.present:
        return OUTCOME_NO_EVIDENCE
    if evidence.parse_error:
        return OUTCOME_REPORT_CORRUPT
    if not evidence.matched:
        return OUTCOME_REPORT_MISMATCH
    counts = evidence.counts
    if (counts.get("collection_errors") or 0) > 0:
        return OUTCOME_INFRASTRUCTURE_ERROR
    executed = counts.get("executed") or 0
    if executed == 0:
        return OUTCOME_NO_TESTS
    if (counts.get("passed") or 0) == 0 and (counts.get("skipped") or 0) >= executed:
        return OUTCOME_SKIPPED_ONLY
    if (counts.get("failed") or 0) > 0:
        return OUTCOME_FAILED
    if (counts.get("errors") or 0) > 0:
        return OUTCOME_INFRASTRUCTURE_ERROR
    if run.returncode != 0:
        return OUTCOME_INFRASTRUCTURE_ERROR  # every listed test passed but the runner failed anyway: not a pass
    return OUTCOME_PASSED
