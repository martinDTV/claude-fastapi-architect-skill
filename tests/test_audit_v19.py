"""v1.9: verified remediation, hardened. One isolated copy per finding, a linked reproducer per case, trust-scoped
execution through an executor, states with reason codes, and adversarial controls: suppression instead of a
fix, tampered tests, independent regressions, reproducers that already pass, skipped or missing tests, stale
baselines, mismatched candidates, infrastructure failures, timeouts, configuration errors, denied execution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, ProjectCase, audit, audit_fixture, lines_of

from fastapi_audit.remediate import RECIPE_META, RECIPES, Authorization, RemediationCase, RemediationError, RunOptions, recipe_for, run_remediation  # noqa: E402
from fastapi_audit.remediate import pipeline as pl  # noqa: E402
from fastapi_audit.remediate.executors import REGISTRY, LocalProcessExecutor, select_executor  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ALLOWED_COMMANDS, ExecutionPolicy, Workspace, command_allowed, digest_tree, interpret_run, run_command  # noqa: E402
from fastapi_audit.remediate.runners import ADAPTERS, Evidence, PytestJUnitAdapter, UnittestJsonAdapter, adapter_for, assess  # noqa: E402
from fastapi_audit.remediate.model import (  # noqa: E402
    OUTCOME_FAILED,
    OUTCOME_INFRASTRUCTURE_ERROR,
    OUTCOME_NO_EVIDENCE,
    OUTCOME_NO_TESTS,
    OUTCOME_PASSED,
    OUTCOME_REPORT_CORRUPT,
    OUTCOME_REPORT_MISMATCH,
    OUTCOME_SKIPPED_ONLY,
    OUTCOME_TIMEOUT,
    OUTCOME_UNVERIFIABLE,
    STATE_AUDIT_CLEAN,
    STATE_BLOCKED,
    STATE_INCONCLUSIVE,
    STATE_PATCHED,
    STATE_REJECTED,
    STATE_VERIFIED,
    STATES,
    AuditCheck,
    Edit,
    PatchProposal,
    TestRun,
)
from fastapi_audit.remediate.patches import Blocked, apply_edits, propose  # noqa: E402
from fastapi_audit.remediate.render import render_markdown, render_text  # noqa: E402

FIXTURE = FIXTURES / "remediation19"
REMEDIATE_SHIM = REPO_ROOT / "scripts" / "audit_remediate.py"
PY = sys.executable
SUITE = f"{PY} -m unittest discover -s tests -t . -q"
LINKS = {
    "TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_defects.DefectTests.test_import_is_atomic_when_an_item_fails tests.test_defects.DefectTests.test_import_commits_everything_once_on_success",
    "TX_COMMIT_INSIDE_BEGIN_BLOCK": f"{PY} -m unittest tests.test_defects.DefectTests.test_reprice_commits_both_writes_without_error",
    "SA_ASYNC_SESSION_NOT_AWAITED": f"{PY} -m unittest tests.test_defects.DefectTests.test_audit_trail_executes_the_statement",
}


def _digest(root: Path) -> dict:
    return digest_tree(str(root))


def fixture_options(root: Path = FIXTURE, links=None, suite: bool = True, trust: str = "fixture", execution: bool = True, timeout_s: float = 180.0, **kw) -> RunOptions:
    policy = ExecutionPolicy(authorized=execution, command=SUITE if suite else None, timeout_s=timeout_s)
    return RunOptions(policy=policy, authorization=Authorization(execution=execution, trust=trust, scope=str(root), declared_by="test"), links=dict(LINKS if links is None else links), **kw)


# --------------------------------------------------------------------------- #
# the vertical slice on the fixture
# --------------------------------------------------------------------------- #


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.before = _digest(FIXTURE)
        cls.static = run_remediation(str(FIXTURE), RunOptions())
        cls.denied = run_remediation(str(FIXTURE), fixture_options(trust="untrusted"))
        cls.verified = run_remediation(str(FIXTURE), fixture_options())

    def test_the_fixture_reproduces_its_defects_and_is_never_modified(self) -> None:
        proc = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], cwd=str(FIXTURE), capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(_digest(FIXTURE), self.before)
        for run in (self.static, self.denied, self.verified):
            self.assertTrue(run["original_untouched"])
            self.assertTrue(all(c["applied_to_project"] is False for c in run["cases"]))

    def test_static_run_executes_nothing_and_says_why(self) -> None:
        run = self.static
        self.assertFalse(run["execution"]["granted"])
        self.assertEqual(run["execution"]["denied_code"], "execution_unauthorized")
        self.assertEqual(run["summary"]["by_state"], {STATE_AUDIT_CLEAN: 3})
        self.assertEqual(run["summary"]["by_reason_code"], {"execution_unauthorized": 3})
        for case in run["cases"]:
            self.assertFalse(case["linked_tests"]["before"]["ran"] or case["linked_tests"]["after"]["ran"])
            self.assertIsNone(case["defect_reproduced"])
            self.assertTrue(case["verification"]["before_audit"]["target_present"])
            self.assertFalse(case["verification"]["after_audit"]["target_present"])
            self.assertEqual(case["regressions"], [])
            self.assertTrue(any("no test executed" in l for l in case["limitations"]))
            self.assertEqual([i["satisfied"] for i in case["invariants"] if i["checked_by"] in ("audit", "parse")], [True, True, True])
            self.assertTrue(all(i["satisfied"] is None for i in case["invariants"] if i["checked_by"] == "not_checked"))

    def test_an_untrusted_project_fails_closed_even_when_authorized(self) -> None:
        run = self.denied
        self.assertTrue(run["authorization"]["execution"])
        self.assertFalse(run["authorization"]["technical_guarantee"])
        self.assertFalse(run["execution"]["granted"])
        self.assertEqual(run["execution"]["denied_code"], "executor_unavailable")
        self.assertIn("not isolation", run["execution"]["denied_reason"])
        self.assertEqual(run["summary"]["by_state"], {STATE_AUDIT_CLEAN: 3})
        self.assertEqual(run["summary"]["by_reason_code"], {"executor_unavailable": 3})
        self.assertTrue(all(not c["linked_tests"]["before"]["ran"] and c["executor"] is None for c in run["cases"]))
        self.assertEqual(run["combined"]["suite"]["after"]["ran"], False)
        self.assertEqual(run["combined"]["state"], "inconclusive")  # audited only: never inferred from individual verdicts

    def test_fixture_trust_with_linked_reproducers_verifies_each_case(self) -> None:
        run = self.verified
        self.assertTrue(run["execution"]["granted"])
        self.assertEqual(run["execution"]["executor"]["isolation_level"], "process_hygiene")
        self.assertIn("network isolation", run["execution"]["executor"]["not_provided"])
        self.assertEqual(run["summary"]["by_state"], {STATE_VERIFIED: 3})
        self.assertEqual(run["summary"]["composition"], "verified")
        combined = run["combined"]
        self.assertEqual(combined["suite"]["before"]["outcome"], OUTCOME_FAILED)
        self.assertEqual(combined["suite"]["after"]["outcome"], OUTCOME_PASSED)
        self.assertTrue(combined["suite"]["after"]["evidence"]["verifiable"])
        self.assertTrue(combined["audit"]["ok"])
        self.assertEqual(len(combined["applied"]), 3)
        self.assertEqual({a["candidate_id"] for a in combined["applied"]}, {c["candidate_id"] for c in run["cases"]})
        self.assertTrue(all(v["outcome"] == OUTCOME_PASSED for v in combined["reproducers"].values()))
        for case in run["cases"]:
            self.assertEqual(case["reproducer"], LINKS[case["rule"]])
            self.assertEqual(case["reproducer_scope"], "rule")
            self.assertEqual(case["shared_with"], [])  # one case per rule in the fixture: nothing shared
            self.assertTrue(case["defect_reproduced"])
            self.assertTrue(case["flipped_tests"])
            self.assertEqual(case["regressed_tests"], [])
            for run_key in ("before", "after"):
                ev = case["linked_tests"][run_key]["evidence"]
                self.assertEqual(ev["adapter"], "unittest-json")
                self.assertTrue(ev["verifiable"])
                self.assertGreaterEqual(ev["counts"]["executed"], 1)
            self.assertEqual((case["linked_tests"]["before"]["outcome"], case["linked_tests"]["after"]["outcome"]), (OUTCOME_FAILED, OUTCOME_PASSED))
            self.assertEqual(case["linked_tests"]["after"]["executor"], "local")
            self.assertEqual((case["executor"], case["isolation_level"]), ("local", "process_hygiene"))
            self.assertTrue(case["tests_untouched"])
            self.assertTrue(all(i["satisfied"] for i in case["invariants"]))
            self.assertEqual(case["candidate_id"], case["patch"]["candidate_id"])
            self.assertEqual(case["base_sha256"], case["patch"]["original_sha256"])
            self.assertEqual([h["to"] for h in case["history"]], [STATE_PATCHED, STATE_VERIFIED])
            self.assertTrue(any("does not provide: network isolation" in l for l in case["limitations"]))

    def test_without_linked_reproducers_the_suite_alone_is_inconclusive(self) -> None:
        run = run_remediation(str(FIXTURE), fixture_options(links={}))
        self.assertEqual(run["summary"]["by_state"], {STATE_INCONCLUSIVE: 3})
        self.assertEqual(run["summary"]["by_reason_code"], {"no_linked_tests": 3})
        self.assertEqual(run["combined"]["state"], "not_run")  # inconclusive candidates are not composed
        self.assertEqual(run["summary"]["composition"], "not_run")

    def test_patches_apply_cleanly_to_the_original_and_reproduce_the_verified_copy(self) -> None:
        source = (FIXTURE / "shop" / "service.py").read_text(encoding="utf-8")
        patched = source
        for case in sorted(self.verified["cases"], key=lambda c: -c["line"]):  # every candidate is computed against the audited file
            edits = [Edit(e["line"], e["kind"], e["text"]) for e in case["patch"]["edits"]]
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), case["patch"]["original_sha256"])
            self.assertEqual(hashlib.sha256(apply_edits(source, edits).encode()).hexdigest(), case["patch"]["patched_sha256"])
            patched = apply_edits(patched, edits)  # candidates at different lines compose, highest line first
        self.assertIn("    for item in items:\n        session.add(item)\n    await session.commit()\n", patched)
        self.assertIn("        session.add(item)\n        session.add(adjustment)\n", patched)
        self.assertIn("    await session.execute(message)\n", patched)
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, Path(tmp) / "p")
            for case in sorted(self.verified["cases"], key=lambda c: -c["line"]):
                proc = subprocess.run(["git", "apply", "--check", "-"], cwd=str(Path(tmp) / "p"), input=case["patch"]["diff"], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                subprocess.run(["git", "apply", "-"], cwd=str(Path(tmp) / "p"), input=case["patch"]["diff"], capture_output=True, text=True, check=True)
            self.assertEqual((Path(tmp) / "p" / "shop" / "service.py").read_text(encoding="utf-8"), patched)
            proc = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], cwd=str(Path(tmp) / "p"), capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])

    def test_the_audit_report_names_the_recipe_and_keeps_the_v18_contract(self) -> None:
        report = audit_fixture("remediation19")
        blocks = {f["rule"]: f["remediation"] for f in report["findings"] if f["remediation"]}
        self.assertEqual({r: (b["recipe"], b["automatable"], b["tool"]) for r, b in blocks.items()}, {
            "TX_COMMIT_IN_LOOP": ("move_commit_after_loop", True, "scripts/audit_remediate.py"),
            "TX_COMMIT_INSIDE_BEGIN_BLOCK": ("remove_commit_inside_begin", True, "scripts/audit_remediate.py"),
            "SA_ASYNC_SESSION_NOT_AWAITED": ("add_await", True, "scripts/audit_remediate.py"),
        })
        for block in blocks.values():  # the v1.8 proposal contract is intact
            self.assertEqual((block["status"], block["applied"], block["verified"]), ("proposed", False, False))
            for key in ("problem", "evidence", "certainty", "proposed_fix", "invariants", "tests", "regression_risk"):
                self.assertIn(key, block)
        self.assertEqual(report["schema_version"], "1.7")

    def test_renderers_selection_and_recipe_metadata(self) -> None:
        text = render_text(self.verified)
        self.assertIn("cases: 3  verified 3  composition: verified", text)
        self.assertIn("reproducer (rule):", text)
        self.assertIn("flipped:", text)
        md = render_markdown(self.verified)
        self.assertIn("```diff", md)
        self.assertIn("(`verified`)", md)
        target = self.static["cases"][0]["fingerprint"]
        one = run_remediation(str(FIXTURE), RunOptions(fingerprints={target}))
        self.assertEqual([c["fingerprint"] for c in one["cases"]], [target])
        self.assertEqual(set(RECIPE_META), set(RECIPES.values()))
        for meta in RECIPE_META.values():
            self.assertIn(meta["maturity"], {"postgresql-verified", "fake-verified", "experimental"})
            self.assertTrue(any(kind == "linked_tests" for _s, kind in meta["invariants"]))


# --------------------------------------------------------------------------- #
# adversarial controls: each one must be rejected or inconclusive with a distinct reason code
# --------------------------------------------------------------------------- #


class AdversarialTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)
        self._restore = []
        self.addCleanup(self._restore_all)
        self.workspaces = []
        original_create = Workspace.create

        def recording_create(project_root, workspace_dir=None, ignored=Workspace.ignored):
            ws = original_create(project_root, workspace_dir, ignored)
            self.workspaces.append(ws)
            return ws

        self.patch_attr(Workspace, "create", staticmethod(recording_create))

    def patch_attr(self, obj, name, value) -> None:
        self._restore.append((obj, name, obj.__dict__.get(name, getattr(obj, name))))
        setattr(obj, name, value)

    def _restore_all(self) -> None:
        for obj, name, value in reversed(self._restore):
            setattr(obj, name, value)

    def options(self, **kw) -> RunOptions:
        return fixture_options(root=self.root, **kw)

    def run_rule(self, rule: str, **kw):
        return run_remediation(str(self.root), self.options(select_rules={rule}, **kw))

    def test_suppressing_the_finding_instead_of_fixing_it_is_rejected(self) -> None:
        real = pl.apply_edits

        def cheat(source, edits):
            lines = source.splitlines(keepends=True)
            for e in edits:
                if e.kind == "delete":
                    lines[e.lineno - 1] = lines[e.lineno - 1].rstrip("\n") + "  # audit-fastapi: ignore\n"
                    return "".join(lines)
            return real(source, edits)

        self.patch_attr(pl, "apply_edits", cheat)
        run = self.run_rule("TX_COMMIT_INSIDE_BEGIN_BLOCK")
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_REJECTED, "finding_persists"))
        self.assertIn("suppressed rather than fixed", case["reason"])
        self.assertTrue(case["verification"]["after_audit"]["target_suppressed"])

    def test_a_candidate_that_edits_a_test_file_is_rejected(self) -> None:
        real = pl.apply_edits

        def tamper(source, edits):
            ws = self.workspaces[-1]
            target = ws.project / "tests" / "test_defects.py"
            target.write_text(target.read_text(encoding="utf-8").replace("self.assertEqual(session.commits, 0,", "self.assertEqual(session.commits, 2,"), encoding="utf-8")
            return real(source, edits)

        self.patch_attr(pl, "apply_edits", tamper)
        run = self.run_rule("TX_COMMIT_IN_LOOP")
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_REJECTED, "tests_modified"))
        self.assertIn("tests/test_defects.py", case["reason"])
        self.assertFalse(case["tests_untouched"])

    def test_a_candidate_that_introduces_an_independent_regression_is_rejected(self) -> None:
        real = pl.apply_edits

        def regress(source, edits):
            patched = real(source, edits)
            return patched.replace("        session.add(item)\n    await session.commit()\n    return len(items)", "        session.add(item)\n    session.execute(\"regression\")\n    await session.commit()\n    return len(items)")

        self.patch_attr(pl, "apply_edits", regress)
        run = self.run_rule("TX_COMMIT_IN_LOOP")
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_REJECTED, "regression_introduced"))
        self.assertEqual([r["rule"] for r in case["regressions"]], ["SA_ASYNC_SESSION_NOT_AWAITED"])

    def test_a_reproducer_that_already_passes_does_not_verify(self) -> None:
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_defects.DefectTests.test_summary_control_is_untouched"})
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "defect_not_reproduced"))
        self.assertFalse(case["defect_reproduced"])
        self.assertFalse(case["linked_tests"]["after"]["ran"])
        self.assertFalse(case["verification"]["after_audit"]["target_present"])  # a clean audit is still not verified

    def test_a_reproducer_that_fails_before_and_after_does_not_observe_the_candidate(self) -> None:
        self.write("tests/test_always_fails.py", '''
            import unittest

            class T(unittest.TestCase):
                def test_x(self):
                    self.fail("independent failure")
        ''')
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_always_fails"}, suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "reproducer_not_specific"))
        self.assertTrue(case["defect_reproduced"])  # it failed before, but the same test fails after: no demonstrable link
        self.assertEqual((case["flipped_tests"], case["regressed_tests"]), ([], []))

    def test_a_test_that_passed_before_and_fails_after_rejects_the_candidate(self) -> None:
        self.write("tests/test_old_expectation.py", '''
            import asyncio
            import unittest
            import warnings

            from tests.fakes import FakeAsyncSession, stub_sqlalchemy_if_missing

            stub_sqlalchemy_if_missing()

            from shop import service  # noqa: E402


            class OldExpectation(unittest.TestCase):
                def test_import_commits_three_times(self):  # encodes the defective behaviour: one commit per item
                    session = FakeAsyncSession()
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        asyncio.run(service.import_items(session, ["a", "b", "c"]))
                    self.assertEqual(session.commits, 3)
        ''')
        reproducer = f"{PY} -m unittest tests.test_defects.DefectTests.test_import_is_atomic_when_an_item_fails tests.test_old_expectation.OldExpectation.test_import_commits_three_times"
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": reproducer}, suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_REJECTED, "tests_failed_after"))
        self.assertEqual(case["flipped_tests"], ["tests.test_defects.DefectTests.test_import_is_atomic_when_an_item_fails"])
        self.assertEqual(case["regressed_tests"], ["tests.test_old_expectation.OldExpectation.test_import_commits_three_times"])
        self.assertIn("tests that passed before now fail", case["reason"])

    def test_an_after_run_that_does_not_execute_is_inconclusive(self) -> None:
        calls = {"n": 0}
        real_run = LocalProcessExecutor.run

        def flaky(self_exec, command, workspace, policy):
            calls["n"] += 1
            if calls["n"] == 2:
                return interpret_run(TestRun(ran=False, command=command.split(), refused="executor unavailable during the after run"))
            return real_run(self_exec, command, workspace, policy)

        self.patch_attr(LocalProcessExecutor, "run", flaky)
        run = self.run_rule("TX_COMMIT_IN_LOOP", suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "command_refused"))
        self.assertIn("after the patch", case["reason"])

    def test_skipped_and_missing_tests_never_satisfy_the_obligation(self) -> None:
        self.write("tests/test_skipped.py", '''
            import unittest

            class T(unittest.TestCase):
                @unittest.skip("not today")
                def test_x(self):
                    pass
        ''')
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_skipped"}, suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_INCONCLUSIVE, "tests_skipped"))
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_defects.DefectTests.test_does_not_exist"}, suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "infrastructure_error"))
        self.assertEqual(case["linked_tests"]["before"]["evidence"]["counts"]["collection_errors"], 1)  # the selector did not resolve

    def test_runners_without_an_adapter_never_verify_whatever_their_exit_code(self) -> None:
        self.write("tests/green_script.py", "import sys\nprint('3 passed in 0.01s')\nsys.exit(0)\n")  # a script that imitates a pytest summary
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} tests/green_script.py"}, suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_INCONCLUSIVE, "runner_unverifiable"))
        self.assertEqual(run["cases"][0]["linked_tests"]["before"]["outcome"], OUTCOME_UNVERIFIABLE)
        self.assertFalse(run["cases"][0]["linked_tests"]["before"]["evidence"]["verifiable"])
        self.write("tests/red_script.py", "import sys\nprint('1 error in 0.30s')\nsys.exit(1)\n")
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} tests/red_script.py"}, suite=False)
        self.assertEqual(run["cases"][0]["reason_code"], "runner_unverifiable")
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -c \"import time; time.sleep(30)\""}, suite=False, timeout_s=1.0)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_INCONCLUSIVE, "timeout"))
        self.assertEqual(run["cases"][0]["linked_tests"]["before"]["outcome"], OUTCOME_TIMEOUT)

    def test_exit_code_zero_without_executed_tests_is_not_evidence(self) -> None:
        self.write("tests/test_nothing.py", "import unittest\n\nclass T(unittest.TestCase):\n    pass\n")
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": f"{PY} -m unittest tests.test_nothing"}, suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "no_tests"))
        self.assertEqual(case["linked_tests"]["before"]["evidence"]["counts"]["executed"], 0)
        self.assertEqual(case["linked_tests"]["before"]["evidence"]["counts"]["discovered"], 0)

    def test_corrupt_or_foreign_reports_are_inconclusive(self) -> None:
        real_collect = UnittestJsonAdapter.collect

        def corrupt(self_adapter, report, nonce, started_at):
            Path(report).write_text("{not json", encoding="utf-8")
            return real_collect(self_adapter, report, nonce, started_at)

        self.patch_attr(UnittestJsonAdapter, "collect", corrupt)
        run = self.run_rule("TX_COMMIT_IN_LOOP", suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "report_corrupt"))
        self.assertEqual(case["linked_tests"]["before"]["outcome"], OUTCOME_REPORT_CORRUPT)
        self._restore_all()
        self._restore = []

        def foreign(self_adapter, report, nonce, started_at):
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            data["nonce"] = "another-run"
            Path(report).write_text(json.dumps(data), encoding="utf-8")
            return real_collect(self_adapter, report, nonce, started_at)

        self.patch_attr(UnittestJsonAdapter, "collect", foreign)
        run = self.run_rule("TX_COMMIT_IN_LOOP", suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_INCONCLUSIVE, "report_mismatch"))
        self._restore_all()
        self._restore = []

        def missing(self_adapter, report, nonce, started_at):
            Path(report).unlink(missing_ok=True)
            return real_collect(self_adapter, report, nonce, started_at)

        self.patch_attr(UnittestJsonAdapter, "collect", missing)
        run = self.run_rule("TX_COMMIT_IN_LOOP", suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_INCONCLUSIVE, "evidence_missing"))

    def test_two_findings_of_one_rule_with_a_reproducer_that_covers_one(self) -> None:
        """Rule-level links are shared: the uncovered finding must not inherit the covered one's verdict."""
        service = self.root / "shop" / "service.py"
        service.write_text(service.read_text(encoding="utf-8") + '''

async def import_items_b(session: AsyncSession, items: list) -> None:
    """A second per-item loop, not observed by any test."""
    for item in items:
        session.add(item)
        await session.commit()
''', encoding="utf-8")
        run = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": LINKS["TX_COMMIT_IN_LOOP"]})
        by_function = {c["function"]: c for c in run["cases"]}
        self.assertEqual(set(by_function), {"import_items", "import_items_b"})
        covered, uncovered = by_function["import_items"], by_function["import_items_b"]
        self.assertEqual(covered["state"], STATE_VERIFIED)
        self.assertTrue(covered["flipped_tests"])
        self.assertEqual((uncovered["state"], uncovered["reason_code"]), (STATE_INCONCLUSIVE, "reproducer_not_specific"))
        self.assertEqual(uncovered["flipped_tests"], [])
        self.assertEqual(sorted(covered["shared_with"]), [uncovered["fingerprint"]])
        self.assertEqual(sorted(uncovered["shared_with"]), [covered["fingerprint"]])
        self.assertTrue(any("shared with" in l for l in uncovered["limitations"]))
        self.assertEqual(run["combined"]["cases"], [covered["fingerprint"]])  # composed from the verified candidate only
        # the fixture's other defects are still present (only this rule was selected), so the regression suite of
        # the corrected code does not pass: the composition is inconclusive and says which tests failed
        self.assertEqual(run["combined"]["state"], "inconclusive")
        self.assertIn("regression suite did not pass", run["combined"]["cause"])
        self.assertIn("test_reprice_commits_both_writes_without_error", run["combined"]["cause"])
        alone = self.run_rule("TX_COMMIT_IN_LOOP", links={"TX_COMMIT_IN_LOOP": LINKS["TX_COMMIT_IN_LOOP"]}, suite=False)
        self.assertEqual((alone["combined"]["state"], "no regression suite declared" in alone["combined"]["cause"]), ("inconclusive", True))

    def test_composition_is_not_inferred_from_individual_verdicts(self) -> None:
        real_run = LocalProcessExecutor.run

        def fail_in_combined(self_exec, command, workspace, policy):
            result = real_run(self_exec, command, workspace, policy)
            if workspace.root.name.endswith("combined") or "combined" in str(workspace.root):
                return result
            return result

        # make the suite command fail only in the composed copy by linking a suite that passes individually but
        # whose composed run is replaced by a failing one
        calls = {"suite": 0}

        def composed_suite_fails(self_exec, command, workspace, policy):
            result = real_run(self_exec, command, workspace, policy)
            if command == SUITE:
                calls["suite"] += 1
                if calls["suite"] == 2:  # the after run in the composed copy
                    result.outcome, result.passed = OUTCOME_FAILED, False
                    result.evidence = dict(result.evidence, tests={"failed": ["tests.test_defects.DefectTests.test_summary_control_is_untouched"], "passed": [], "errors": [], "skipped": [], "collection_errors": []})
            return result

        self.patch_attr(LocalProcessExecutor, "run", composed_suite_fails)
        run = run_remediation(str(self.root), self.options())
        self.assertEqual(run["summary"]["by_state"], {STATE_VERIFIED: 3})  # individual verdicts stand
        self.assertEqual(run["combined"]["state"], "inconclusive")
        self.assertIn("regression suite did not pass", run["combined"]["cause"])
        self.assertIn("test_summary_control_is_untouched", run["combined"]["cause"])
        self.assertTrue(all(any("composition inconclusive" in l for l in c["limitations"]) for c in run["cases"]))
        self._restore_all()
        self._restore = []

        def reproducer_fails_in_combination(self_exec, command, workspace, policy):
            result = real_run(self_exec, command, workspace, policy)
            if command == LINKS["SA_ASYNC_SESSION_NOT_AWAITED"] and (workspace.root / "project" / "shop" / "service.py").read_text(encoding="utf-8").count("await session.commit()") == 2:
                # the composed copy (all three candidates applied) is the only one where both other commits are also fixed
                result.outcome, result.passed = OUTCOME_FAILED, False
            return result

        self.patch_attr(LocalProcessExecutor, "run", reproducer_fails_in_combination)
        run = run_remediation(str(self.root), self.options())
        self.assertEqual(run["combined"]["state"], "incompatible")
        self.assertIn("do not pass in the composed copy", run["combined"]["cause"])

    def test_a_stale_baseline_invalidates_the_candidate(self) -> None:
        recorded = Workspace.create

        def drifting(project_root, workspace_dir=None, ignored=Workspace.ignored):
            ws = recorded(project_root, workspace_dir, ignored)
            target = ws.project / "shop" / "service.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
            return ws

        self.patch_attr(Workspace, "create", staticmethod(drifting))
        run = self.run_rule("SA_ASYNC_SESSION_NOT_AWAITED", suite=False)
        case = run["cases"][0]
        self.assertEqual((case["state"], case["reason_code"]), (STATE_REJECTED, "stale_baseline"))
        self.assertIsNone(case["patch"])

    def test_a_candidate_for_another_file_or_base_is_rejected(self) -> None:
        real = pl.propose

        def other_file(finding, source):
            result = real(finding, source)
            if isinstance(result, Blocked):
                return result
            return PatchProposal(recipe=result.recipe, file="shop/other.py", summary=result.summary, edits=result.edits, diff=result.diff, original_sha256=result.original_sha256, patched_sha256=result.patched_sha256)

        self.patch_attr(pl, "propose", other_file)
        run = self.run_rule("SA_ASYNC_SESSION_NOT_AWAITED", suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_REJECTED, "candidate_mismatch"))

        def other_base(finding, source):
            result = real(finding, source)
            if isinstance(result, Blocked):
                return result
            return PatchProposal(recipe=result.recipe, file=result.file, summary=result.summary, edits=result.edits, diff=result.diff, original_sha256="0" * 64, patched_sha256=result.patched_sha256)

        self.patch_attr(pl, "propose", other_base)
        run = self.run_rule("SA_ASYNC_SESSION_NOT_AWAITED", suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_REJECTED, "candidate_mismatch"))

    def test_a_patch_that_leaves_the_finding_is_rejected(self) -> None:
        self.patch_attr(pl, "apply_edits", lambda source, edits: source)
        run = self.run_rule("SA_ASYNC_SESSION_NOT_AWAITED", suite=False)
        self.assertEqual((run["cases"][0]["state"], run["cases"][0]["reason_code"]), (STATE_REJECTED, "finding_persists"))

    def test_scope_mismatch_and_unregistered_executor_deny_execution(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            run = run_remediation(str(self.root), RunOptions(policy=ExecutionPolicy(authorized=True), authorization=Authorization(execution=True, trust="trusted", scope=elsewhere), links=dict(LINKS)))
        self.assertEqual((run["execution"]["granted"], run["execution"]["denied_code"]), (False, "scope_mismatch"))
        self.assertEqual(run["summary"]["by_reason_code"], {"scope_mismatch": 3})
        run = run_remediation(str(self.root), RunOptions(policy=ExecutionPolicy(authorized=True), authorization=Authorization(execution=True, trust="trusted", scope=str(self.root)), executor="docker", links=dict(LINKS)))
        self.assertEqual(run["execution"]["denied_code"], "executor_unavailable")
        self.assertIn("not registered", run["execution"]["denied_reason"])

    def test_configuration_errors_stop_the_run_before_any_copy(self) -> None:
        self.write(".audit-fastapi.json", json.dumps({"transactions": {"single_transaction_per_use_case": "yes"}}))
        with self.assertRaises(ValueError):
            run_remediation(str(self.root), RunOptions())
        self.assertEqual(self.workspaces, [])

    def test_findings_in_test_code_are_skipped_unless_asked(self) -> None:
        self.write("tests/test_helper.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession

            async def helper(session: AsyncSession) -> None:
                session.execute("select 1")
        ''')
        run = run_remediation(str(self.root), RunOptions(select_rules={"SA_ASYNC_SESSION_NOT_AWAITED"}))
        self.assertEqual([c["file"] for c in run["cases"]], ["shop/service.py"])
        run = run_remediation(str(self.root), RunOptions(select_rules={"SA_ASYNC_SESSION_NOT_AWAITED"}, include_tests=True))
        self.assertEqual(sorted(c["file"] for c in run["cases"]), ["shop/service.py", "tests/test_helper.py"])
        helper = next(c for c in run["cases"] if c["file"] == "tests/test_helper.py")
        self.assertTrue(any("test code" in l for l in helper["limitations"]))

    def test_the_run_fails_loudly_if_the_project_changes_underneath(self) -> None:
        real = pl.digest_tree
        calls = {"n": 0}

        def flaky(root, *a, **k):
            out = real(root, *a, **k)
            if Path(root).resolve() == self.root:
                calls["n"] += 1
                if calls["n"] == 2:
                    out = dict(out, **{"shop/service.py": "0" * 64})
            return out

        self.patch_attr(pl, "digest_tree", flaky)
        with self.assertRaises(RemediationError):
            run_remediation(str(self.root), RunOptions())


# --------------------------------------------------------------------------- #
# model, interpretation, executors, isolation, recipes, CLI
# --------------------------------------------------------------------------- #


def _full_case() -> RemediationCase:
    case = RemediationCase(fingerprint="f", rule="TX_COMMIT_IN_LOOP", file="m.py", line=1, function="f", recipe="move_commit_after_loop", base_sha256="b")
    case.patch = PatchProposal(recipe="move_commit_after_loop", file="m.py", summary="s", edits=[], diff="--- a/m.py\n+++ b/m.py\n", original_sha256="b", patched_sha256="c")
    case.reproducer = "pytest -q tests/test_x.py"
    verifiable = {"verifiable": True, "adapter": "pytest-junit", "tests": {}}
    case.linked_before = TestRun(ran=True, outcome=OUTCOME_FAILED, passed=False, returncode=1, evidence=dict(verifiable))
    case.linked_after = TestRun(ran=True, outcome=OUTCOME_PASSED, passed=True, returncode=0, evidence=dict(verifiable))
    case.flipped_tests = ["tests/test_x.py::test_commit_once"]
    case.after_audit = AuditCheck(target_present=False, target_suppressed=False, findings_total=0)
    case.tests_untouched = True
    case.executor = "local"
    return case


class ModelTests(unittest.TestCase):
    def test_legal_and_illegal_transitions(self) -> None:
        case = _full_case()
        case.transition(STATE_PATCHED, "applied")
        case.transition(STATE_AUDIT_CLEAN, "clean")
        case.transition(STATE_VERIFIED, "ok")
        self.assertEqual([h["to"] for h in case.history], [STATE_PATCHED, STATE_AUDIT_CLEAN, STATE_VERIFIED])
        self.assertEqual(case.history[-1]["reason_code"], "verified")
        for terminal in (STATE_VERIFIED, STATE_REJECTED, STATE_BLOCKED, STATE_INCONCLUSIVE):
            c = _full_case()
            if terminal == STATE_VERIFIED:
                c.transition(STATE_PATCHED)
            c.transition(terminal, "x", "code")
            for target in STATES:
                with self.assertRaises(RemediationError):
                    c.transition(target)
        fresh = RemediationCase(fingerprint="g", rule="X", file="m.py", line=1, function=None, recipe=None)
        for target in (STATE_VERIFIED, STATE_AUDIT_CLEAN, "done"):
            with self.assertRaises(RemediationError):
                fresh.transition(target)
        self.assertEqual(set(STATES), {"proposed", "blocked", "patched", "audit_clean", "verified", "rejected", "inconclusive"})

    def test_verified_requires_every_piece_of_evidence(self) -> None:
        base = _full_case()
        base.transition(STATE_PATCHED)
        self.assertEqual(base.missing_evidence_for_verified(), [])
        scenarios = {
            "finding present": lambda c: setattr(c, "after_audit", AuditCheck(target_present=True)),
            "finding suppressed": lambda c: setattr(c, "after_audit", AuditCheck(target_present=False, target_suppressed=True)),
            "new findings": lambda c: setattr(c, "after_audit", AuditCheck(target_present=False, target_suppressed=False, new_findings=[{"rule": "X"}])),
            "tests modified": lambda c: setattr(c, "tests_untouched", False),
            "no reproducer": lambda c: setattr(c, "reproducer", None),
            "reproducer passed before": lambda c: setattr(c, "linked_before", TestRun(ran=True, outcome=OUTCOME_PASSED, passed=True, returncode=0)),
            "reproducer skipped before": lambda c: setattr(c, "linked_before", TestRun(ran=True, outcome=OUTCOME_SKIPPED_ONLY, passed=False, returncode=0)),
            "after not run": lambda c: setattr(c, "linked_after", TestRun()),
            "after skipped": lambda c: setattr(c, "linked_after", TestRun(ran=True, outcome=OUTCOME_SKIPPED_ONLY, passed=False, returncode=0)),
            "after infrastructure": lambda c: setattr(c, "linked_after", TestRun(ran=True, outcome=OUTCOME_INFRASTRUCTURE_ERROR, passed=False, returncode=3)),
            "no executor": lambda c: setattr(c, "executor", None),
            "no patch": lambda c: setattr(c, "patch", None),
            "no flipped test": lambda c: setattr(c, "flipped_tests", []),
            "regressed test": lambda c: setattr(c, "regressed_tests", ["tests/test_y.py::test_other"]),
            "before without verifiable evidence": lambda c: setattr(c, "linked_before", TestRun(ran=True, outcome=OUTCOME_FAILED, passed=False, returncode=1)),
            "after without verifiable evidence": lambda c: setattr(c, "linked_after", TestRun(ran=True, outcome=OUTCOME_PASSED, passed=True, returncode=0)),
        }
        for name, mutate in scenarios.items():
            case = _full_case()
            case.transition(STATE_PATCHED)
            mutate(case)
            self.assertTrue(case.missing_evidence_for_verified(), name)
            with self.assertRaises(RemediationError, msg=name):
                case.transition(STATE_VERIFIED)
            self.assertEqual(case.state, STATE_PATCHED, name)
            case.transition(STATE_INCONCLUSIVE, name, "evidence_insufficient")
        self.assertFalse(_full_case().to_dict()["applied_to_project"])


class EvidenceTests(unittest.TestCase):
    """The verdict comes from the runner's own report bound to the run, never from the exit code or stdout."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_for(self, code: int, argv) -> TestRun:
        return TestRun(ran=True, command=list(argv), returncode=code, passed=code == 0)

    def junit(self, body: str, name: str = "N1") -> Path:
        path = self.tmp / "r.xml"
        path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="{name}" tests="{body.count("<testcase")}" errors="0" failures="0" skipped="0">{body}</testsuite></testsuites>')
        return path

    def test_adapter_selection(self) -> None:
        self.assertIsInstance(adapter_for(["pytest", "-q"]), PytestJUnitAdapter)
        self.assertIsInstance(adapter_for(["uv", "run", "pytest", "-q", "tests/x.py"]), PytestJUnitAdapter)
        self.assertIsInstance(adapter_for([PY, "-m", "pytest"]), PytestJUnitAdapter)
        self.assertIsInstance(adapter_for([PY, "-m", "unittest", "tests.x"]), UnittestJsonAdapter)
        self.assertIsNone(adapter_for([PY, "tests/pytest_like.py"]))  # a file named after pytest is not pytest
        self.assertIsNone(adapter_for([PY, "-c", "pass"]))
        self.assertEqual([a.name for a in ADAPTERS], ["pytest-junit", "unittest-json"])
        argv, report = PytestJUnitAdapter().instrument(["pytest", "-q", "tests/x.py"], self.tmp, "abc")
        self.assertEqual(argv[:3], ["pytest", "-q", "tests/x.py"])
        self.assertIn(f"--junit-xml={report}", argv)
        self.assertIn("junit_suite_name=abc", argv)
        argv, report = UnittestJsonAdapter().instrument([PY, "-m", "unittest", "discover", "-s", "tests"], self.tmp, "abc")
        self.assertEqual(argv[0], PY)
        self.assertTrue(argv[1].endswith("fa_unittest_runner.py"))
        self.assertEqual(argv[2:6], ["--output", str(report), "--nonce", "abc"])
        self.assertEqual(argv[6:], ["discover", "-s", "tests"])

    def test_junit_report_is_read_strictly(self) -> None:
        adapter = PytestJUnitAdapter()
        ok = adapter.collect(self.junit('<testcase classname="t" name="a"/><testcase classname="t" name="b"><skipped/></testcase>'), "N1", 0.0)
        self.assertTrue(ok.verifiable)
        self.assertEqual((ok.counts["executed"], ok.counts["passed"], ok.counts["skipped"], ok.counts["discovered"]), (2, 1, 1, None))
        self.assertEqual(assess(self.run_for(0, ["pytest"]), ok), OUTCOME_PASSED)
        self.assertEqual(assess(self.run_for(1, ["pytest"]), ok), OUTCOME_INFRASTRUCTURE_ERROR)  # all listed tests passed but the runner failed
        failed = adapter.collect(self.junit('<testcase classname="t" name="a"><failure message="x"/></testcase>'), "N1", 0.0)
        self.assertEqual(assess(self.run_for(1, ["pytest"]), failed), OUTCOME_FAILED)
        errors = adapter.collect(self.junit('<testcase classname="t" name="a"><error message="fixture setup failed"/></testcase>'), "N1", 0.0)
        self.assertEqual(assess(self.run_for(1, ["pytest"]), errors), OUTCOME_INFRASTRUCTURE_ERROR)
        collection = adapter.collect(self.junit('<testcase classname="tests.test_x" name="tests/test_x.py"><error message="collection failure"/></testcase>'), "N1", 0.0)
        self.assertEqual(collection.counts["collection_errors"], 1)
        self.assertEqual(assess(self.run_for(2, ["pytest"]), collection), OUTCOME_INFRASTRUCTURE_ERROR)
        skipped_only = adapter.collect(self.junit('<testcase classname="t" name="a"><skipped/></testcase>'), "N1", 0.0)
        self.assertEqual(assess(self.run_for(0, ["pytest"]), skipped_only), OUTCOME_SKIPPED_ONLY)
        empty = adapter.collect(self.junit(""), "N1", 0.0)
        self.assertEqual(assess(self.run_for(5, ["pytest"]), empty), OUTCOME_NO_TESTS)
        self.assertEqual(assess(self.run_for(0, ["pytest"]), empty), OUTCOME_NO_TESTS)  # exit 0 with nothing executed
        foreign = adapter.collect(self.junit('<testcase classname="t" name="a"/>', name="OTHER"), "N1", 0.0)
        self.assertFalse(foreign.matched)
        self.assertEqual(assess(self.run_for(0, ["pytest"]), foreign), OUTCOME_REPORT_MISMATCH)
        corrupt = self.tmp / "c.xml"
        corrupt.write_text("<testsuites><testsuite name='N1'")
        self.assertEqual(assess(self.run_for(0, ["pytest"]), adapter.collect(corrupt, "N1", 0.0)), OUTCOME_REPORT_CORRUPT)
        self.assertEqual(assess(self.run_for(0, ["pytest"]), adapter.collect(self.tmp / "absent.xml", "N1", 0.0)), OUTCOME_NO_EVIDENCE)
        stale = self.junit('<testcase classname="t" name="a"/>')
        os.utime(stale, (1_000_000, 1_000_000))
        self.assertEqual(assess(self.run_for(0, ["pytest"]), adapter.collect(stale, "N1", 2_000_000.0)), OUTCOME_REPORT_MISMATCH)
        self.assertEqual(assess(self.run_for(0, [PY, "x.py"]), Evidence(adapter=None)), OUTCOME_UNVERIFIABLE)
        self.assertEqual(assess(TestRun(ran=True, command=["pytest"], timed_out=True), None), OUTCOME_TIMEOUT)
        self.assertEqual(assess(TestRun(ran=False, refused="x"), None), "refused")

    def test_unittest_adapter_runs_real_tests_and_reports_them(self) -> None:
        project = self.tmp / "p"
        shutil.copytree(FIXTURE, project)
        (project / "tests" / "test_mixed.py").write_text(textwrap.dedent('''
            import unittest

            class T(unittest.TestCase):
                def test_ok(self): pass
                def test_fail(self): self.fail("no")
                def test_raise(self): raise RuntimeError("boom")
                @unittest.skip("later")
                def test_skip(self): pass
        '''), encoding="utf-8")
        (project / "tests" / "test_broken.py").write_text("import module_that_does_not_exist\n", encoding="utf-8")
        adapter = UnittestJsonAdapter()
        argv, report = adapter.instrument([PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."], self.tmp, "N2")
        proc = subprocess.run(argv, cwd=str(project), capture_output=True, text=True, timeout=120)
        evidence = adapter.collect(report, "N2", 0.0)
        self.assertTrue(evidence.verifiable)
        c = evidence.counts
        self.assertEqual((c["collection_errors"], c["skipped"]), (1, 1))
        self.assertIn("tests.test_mixed.T.test_fail", evidence.tests["failed"])
        self.assertIn("tests.test_mixed.T.test_raise", evidence.tests["failed"])  # an exception is a failed test under unittest semantics
        self.assertEqual(evidence.tests["errors"], [])
        self.assertEqual(c["discovered"], c["selected"] + c["collection_errors"])
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=proc.returncode, passed=proc.returncode == 0), evidence), OUTCOME_INFRASTRUCTURE_ERROR)  # a collection error voids the run
        argv, report = adapter.instrument([PY, "-m", "unittest", "tests.test_mixed.T.test_ok", "tests.test_mixed.T.test_skip"], self.tmp, "N3")
        proc = subprocess.run(argv, cwd=str(project), capture_output=True, text=True, timeout=120)
        evidence = adapter.collect(report, "N3", 0.0)
        self.assertEqual((proc.returncode, evidence.counts["passed"], evidence.counts["skipped"]), (0, 1, 1))
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=0, passed=True), evidence), OUTCOME_PASSED)
        argv, report = adapter.instrument([PY, "-m", "unittest", "tests.test_mixed.T.test_skip"], self.tmp, "N4")
        subprocess.run(argv, cwd=str(project), capture_output=True, text=True, timeout=120)
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=0, passed=True), adapter.collect(report, "N4", 0.0)), OUTCOME_SKIPPED_ONLY)

    @unittest.skipUnless(__import__("importlib").util.find_spec("pytest") is not None, "pytest not installed in this interpreter")
    def test_pytest_adapter_runs_real_tests_and_binds_the_report_to_the_run(self) -> None:
        project = self.tmp / "p"
        project.mkdir()
        (project / "test_a.py").write_text("import pytest\n\ndef test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n\n@pytest.mark.skip\ndef test_skip():\n    pass\n", encoding="utf-8")
        adapter = PytestJUnitAdapter()
        argv, report = adapter.instrument([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_a.py"], self.tmp, "N5")
        proc = subprocess.run(argv, cwd=str(project), capture_output=True, text=True, timeout=120)
        evidence = adapter.collect(report, "N5", 0.0)
        self.assertTrue(evidence.verifiable)
        self.assertEqual((evidence.counts["executed"], evidence.counts["passed"], evidence.counts["failed"], evidence.counts["skipped"]), (3, 1, 1, 1))
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=proc.returncode, passed=False), evidence), OUTCOME_FAILED)
        argv, report = adapter.instrument([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_a.py", "-k", "test_ok"], self.tmp, "N6")
        proc = subprocess.run(argv, cwd=str(project), capture_output=True, text=True, timeout=120)
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=proc.returncode, passed=proc.returncode == 0), adapter.collect(report, "N6", 0.0)), OUTCOME_PASSED)
        self.assertEqual(assess(TestRun(ran=True, command=argv, returncode=0, passed=True), adapter.collect(report, "N7", 0.0)), OUTCOME_REPORT_MISMATCH)  # another run's nonce

    def test_text_summaries_are_supplementary_only(self) -> None:
        run = interpret_run(TestRun(ran=True, command=["pytest", "-q"], returncode=0, passed=True, stdout_tail="3 passed in 0.1s"))
        self.assertEqual(run.text_counts, {"passed": 3})
        self.assertEqual(run.counts, {})  # structured counts come only from an adapter report


class ExecutorTests(unittest.TestCase):
    def test_selection_fails_closed(self) -> None:
        root = str(FIXTURE)
        self.assertEqual(select_executor(Authorization(), root)[2], "execution_unauthorized")
        self.assertEqual(select_executor(Authorization(execution=True, trust="untrusted", scope=root), root)[2], "executor_unavailable")
        self.assertEqual(select_executor(Authorization(execution=True, trust="trusted"), root)[2], "scope_mismatch")
        self.assertEqual(select_executor(Authorization(execution=True, trust="trusted", scope=str(FIXTURE / "shop")), root)[2], "scope_mismatch")
        self.assertEqual(select_executor(Authorization(execution=True, trust="weird", scope=root), root)[2], "executor_unavailable")
        self.assertEqual(select_executor(Authorization(execution=True, trust="trusted", scope=root), root, name="container")[2], "executor_unavailable")
        for trust in ("fixture", "trusted"):
            executor, denial, code = select_executor(Authorization(execution=True, trust=trust, scope=str(FIXTURES)), root)
            self.assertIsNotNone(executor, denial)
            self.assertEqual((executor.name, executor.isolation_level, code), ("local", "process_hygiene", ""))
        self.assertEqual(set(REGISTRY), {"local"})
        self.assertFalse(Authorization(execution=True, trust="trusted", scope=root).to_dict()["technical_guarantee"])
        self.assertTrue(LocalProcessExecutor.not_provided)


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace.create(str(FIXTURE))
        self.addCleanup(self.ws.cleanup)
        self.policy = ExecutionPolicy(authorized=True, timeout_s=60.0)

    def test_the_copy_excludes_git_and_environments_and_has_a_fresh_home(self) -> None:
        self.assertTrue((self.ws.project / "shop" / "service.py").is_file())
        self.assertFalse((self.ws.project / ".git").exists())
        self.assertTrue(self.ws.home.is_dir() and not any(self.ws.home.iterdir()))

    def test_nothing_runs_without_authorization_or_with_a_refused_program(self) -> None:
        self.assertFalse(run_command(ExecutionPolicy(authorized=False), self.ws, SUITE).ran)
        self.assertIsNone(command_allowed([PY, "-m", "pytest"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["python3.13", "-m", "unittest"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["uv", "run", "pytest"], DEFAULT_ALLOWED_COMMANDS))
        for argv in (["bash", "-c", "true"], ["rm", "-rf", "x"], ["make", "test"]):
            self.assertIn("not in the allowed list", command_allowed(argv, DEFAULT_ALLOWED_COMMANDS))
        for argv in (["pytest;rm", "-q"], ["python3&&id", "-c", "x"], ["$(id)", "-c", "x"]):
            self.assertIn("shell metacharacters", command_allowed(argv, DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["python3", "-c", "import os; os.getcwd()"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["make", "test"], DEFAULT_ALLOWED_COMMANDS + ("make",)))
        refused = run_command(self.policy, self.ws, "bash -c true")
        self.assertEqual((refused.ran, refused.outcome), (False, "refused"))

    def test_the_environment_is_sanitised_and_the_cwd_is_the_copy(self) -> None:
        os.environ["FA_SECRET_PROBE"] = "leaked"
        try:
            run = run_command(self.policy, self.ws, f"{PY} -c \"import os, pathlib; print(os.environ.get('FA_SECRET_PROBE')); print(os.environ['HOME']); print(pathlib.Path.cwd())\"")
        finally:
            del os.environ["FA_SECRET_PROBE"]
        self.assertTrue(run.ran and run.passed)
        probe, home, cwd = run.stdout_tail.strip().splitlines()
        self.assertEqual(probe, "None")
        self.assertEqual(Path(home).resolve(), self.ws.home.resolve())
        self.assertEqual(Path(cwd).resolve(), self.ws.project.resolve())

    def test_timeouts_are_enforced_and_reported(self) -> None:
        run = run_command(ExecutionPolicy(authorized=True, timeout_s=1.0), self.ws, f"{PY} -c \"import time; time.sleep(30)\"")
        self.assertTrue(run.ran and run.timed_out and run.passed is False)
        self.assertEqual(run.outcome, OUTCOME_TIMEOUT)

    def test_workspace_refuses_unsafe_locations(self) -> None:
        with self.assertRaises(RemediationError):
            Workspace.create(str(FIXTURE), workspace_dir=str(FIXTURE / "inside"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "occupied").write_text("x")
            with self.assertRaises(RemediationError):
                Workspace.create(str(FIXTURE), workspace_dir=tmp)


class RecipePreconditionTests(unittest.TestCase):
    def finding(self, rule: str, source: str, line: int, col: int, **details):
        return {"rule": rule, "file": "m.py", "line": line, "column": col, "details": details or None}, textwrap.dedent(source)

    def test_add_await_is_blocked_outside_async_functions_and_in_lambdas(self) -> None:
        f, src = self.finding("SA_ASYNC_SESSION_NOT_AWAITED", "def sync(session):\n    session.execute('x')\n", 2, 4)
        self.assertIsInstance(propose(f, src), Blocked)
        f, src = self.finding("SA_ASYNC_SESSION_NOT_AWAITED", "async def a(session):\n    run(lambda: session.execute('x'))\n", 2, 16)
        self.assertIn("lambda", propose(f, src).reason)
        f, src = self.finding("SA_ASYNC_SESSION_NOT_AWAITED", "async def a(session):\n    return session.execute('x')\n", 2, 11)
        proposal = propose(f, src)
        self.assertEqual(proposal.recipe, "add_await")
        self.assertIn("    return await session.execute('x')\n", apply_edits(src, proposal.edits))
        f, src = self.finding("SA_ASYNC_SESSION_NOT_AWAITED", "async def a(session):\n    r = session.execute('x')\n    return r\n", 2, 8)
        self.assertIn("    r = await session.execute('x')\n", apply_edits(src, propose(f, src).edits))

    def test_move_commit_is_blocked_for_nested_commits_returns_and_deliberate_loops(self) -> None:
        nested = "async def f(session, items):\n    for item in items:\n        try:\n            await session.commit()\n        except Exception:\n            pass\n"
        f, src = self.finding("TX_COMMIT_IN_LOOP", nested, 4, 18, loop_kind="per_item", loop_line=2)
        self.assertIn("nested", propose(f, src).reason)
        early = "async def f(session, items):\n    for item in items:\n        session.add(item)\n        await session.commit()\n        if item.last:\n            return 1\n"
        f, src = self.finding("TX_COMMIT_IN_LOOP", early, 4, 14, loop_kind="per_item", loop_line=2)
        self.assertIn("return", propose(f, src).reason)
        f, src = self.finding("TX_COMMIT_IN_LOOP", early, 4, 14, loop_kind="batch", loop_line=2)
        self.assertIsNone(recipe_for(f))
        f, src = self.finding("TX_COMMIT_IN_LOOP", early, 4, 14, loop_kind="batch", loop_line=2, policy="single_transaction_per_use_case")
        self.assertIsNone(recipe_for(f))

    def test_move_commit_handles_a_following_commit_and_a_single_statement_body(self) -> None:
        followed = "async def f(session, items):\n    for item in items:\n        session.add(item)\n        await session.commit()\n    await session.commit()\n"
        f, src = self.finding("TX_COMMIT_IN_LOOP", followed, 4, 14, loop_kind="per_item", loop_line=2)
        self.assertEqual(apply_edits(src, propose(f, src).edits), "async def f(session, items):\n    for item in items:\n        session.add(item)\n    await session.commit()\n")
        lonely = "async def f(session, items):\n    for item in items:\n        await session.commit()\n"
        f, src = self.finding("TX_COMMIT_IN_LOOP", lonely, 3, 14, loop_kind="per_item", loop_line=2)
        self.assertEqual(apply_edits(src, propose(f, src).edits), "async def f(session, items):\n    for item in items:\n        pass\n    await session.commit()\n")

    def test_remove_commit_inside_begin_keeps_blocks_valid_and_refuses_odd_shapes(self) -> None:
        lonely = "async def f(session):\n    async with session.begin():\n        await session.commit()\n"
        f, src = self.finding("TX_COMMIT_INSIDE_BEGIN_BLOCK", lonely, 3, 14)
        self.assertEqual(apply_edits(src, propose(f, src).edits), "async def f(session):\n    async with session.begin():\n        pass\n")
        two = "async def f(session):\n    async with session.begin():\n        session.add(1); await session.commit()\n"
        f, src = self.finding("TX_COMMIT_INSIDE_BEGIN_BLOCK", two, 3, 30)
        self.assertIn("stand alone", propose(f, src).reason)
        moved = "async def f(session):\n    async with session.begin():\n        session.add(1)\n"
        f, src = self.finding("TX_COMMIT_INSIDE_BEGIN_BLOCK", moved, 3, 8)
        self.assertIn("not found", propose(f, src).reason)
        f, src = self.finding("ASYNC_BLOCKING_CALL_INDIRECT", moved, 3, 8)
        self.assertIn("no automatic recipe", propose(f, src).reason)
        self.assertEqual(set(RECIPES), {"TX_COMMIT_IN_LOOP", "TX_COMMIT_INSIDE_BEGIN_BLOCK", "SA_ASYNC_SESSION_NOT_AWAITED"})


@unittest.skipUnless(os.environ.get("FA_RUN_PG_REMEDIATION") == "1" and shutil.which("uv") and shutil.which("initdb"), "set FA_RUN_PG_REMEDIATION=1 with uv and PostgreSQL binaries on PATH")
class PostgresRemediationTests(unittest.TestCase):
    """The three recipes end to end on tenant-ledger, one isolated copy per case, on a throwaway PostgreSQL
    cluster. Opt-in because every copy syncs a virtual environment (minutes). The recorded run is in
    examples/tenant-ledger/VERIFICATION.md."""

    def test_recipes_are_verified_on_postgresql(self) -> None:
        example = REPO_ROOT / "plugins" / "fastapi-architect" / "examples" / "tenant-ledger"
        base = "uv run --frozen --extra dev pytest -q -m remediation tests/test_remediation_targets.py -k "
        anti = "src/ledger/modules/ledger/antipatterns.py"
        links = {
            f"{anti}:176": base + "test_commit_inside_begin_is_remediated",
            f"{anti}:190": base + "test_commit_per_item_inside_begin_is_remediated",
            "SA_ASYNC_SESSION_NOT_AWAITED": base + "note_without_await",
            "TX_COMMIT_IN_LOOP": base + "deposit_each",
        }
        run = run_remediation(str(example), RunOptions(
            policy=ExecutionPolicy(authorized=True, command="uv run --frozen --extra dev pytest -q -m 'remediation or not antipattern_demo'", timeout_s=900.0, env_passthrough=("PATH", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")),
            authorization=Authorization(execution=True, trust="fixture", scope=str(example), declared_by="test"), links=links))
        self.assertTrue(run["original_untouched"])
        self.assertEqual(run["summary"]["by_state"], {STATE_VERIFIED: 4}, json.dumps(run["summary"]))
        self.assertTrue(all(c["defect_reproduced"] and c["flipped_tests"] for c in run["cases"]))
        self.assertTrue(all(c["linked_tests"]["after"]["evidence"]["adapter"] == "pytest-junit" for c in run["cases"]))
        self.assertEqual(run["combined"]["state"], "verified", run["combined"]["cause"])


class CliTests(ProjectCase):
    def test_cli_contract(self) -> None:
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)
        proc = self.cli(".", "--format", "json", "--output", "run.json", "--patch-dir", "patches", script=REMEDIATE_SHIM)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = json.loads(proc.stdout)
        self.assertEqual(run["schema"], "remediation_run/1.2")
        self.assertEqual(run["summary"]["by_state"], {"audit_clean": 3})
        self.assertEqual(json.loads((self.root / "run.json").read_text()), run)
        self.assertEqual(len(list((self.root / "patches").glob("*.diff"))), 3)
        self.assertEqual(self.cli(".", "--test-command", "pytest", script=REMEDIATE_SHIM).returncode, 2)
        self.assertEqual(self.cli(".", "--link", "TX_COMMIT_IN_LOOP=pytest", script=REMEDIATE_SHIM).returncode, 2)
        self.assertEqual(self.cli(".", "--authorize-execution", "--link", "nonsense", script=REMEDIATE_SHIM).returncode, 2)
        self.assertEqual(self.cli(".", "--select", "ASYNC_BLOCKING_CALL_INDIRECT", script=REMEDIATE_SHIM).returncode, 2)
        self.assertEqual(self.cli(".", "--select", "NOT_A_RULE", script=REMEDIATE_SHIM).returncode, 2)
        denied = self.cli(".", "--authorize-execution", "--test-command", SUITE, script=REMEDIATE_SHIM)
        self.assertEqual(denied.returncode, 0, denied.stderr)
        self.assertIn("execution: execution denied", denied.stdout)
        self.assertIn("[executor_unavailable]", denied.stdout)
        self.assertEqual(self.cli(".", "--authorize-execution", "--test-command", SUITE, "--strict", script=REMEDIATE_SHIM).returncode, 1)
        args = [".", "--authorize-execution", "--trust", "fixture", "--test-command", SUITE, "--format", "markdown", "--strict"]
        for rule, command in LINKS.items():
            args += ["--link", f"{rule}={command}"]
        verified = self.cli(*args, script=REMEDIATE_SHIM)
        self.assertEqual(verified.returncode, 0, verified.stdout[-1500:] + verified.stderr)
        self.assertIn("| verified | 3 |", verified.stdout)
        gated = self.cli(".", "--authorize-execution", "--trust", "fixture", "--link", f"TX_COMMIT_IN_LOOP={PY} -m unittest tests.test_defects.DefectTests.test_summary_control_is_untouched", "--select", "TX_COMMIT_IN_LOOP", "--strict", script=REMEDIATE_SHIM)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("defect_not_reproduced", gated.stdout)
        self.assertEqual(_digest(self.root), _digest(FIXTURE))


if __name__ == "__main__":
    unittest.main()
