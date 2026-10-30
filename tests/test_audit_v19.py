"""v1.9: verified remediation. The pipeline audits a project read-only, patches findings with an automatic
recipe inside an isolated copy, re-audits, runs the authorized test command before and after, and reports a
state per case that never claims more than it checked. Negative and adversarial controls: blocked
preconditions, rejected patches, refused commands, sanitised environment, timeouts, untouched project."""

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

from fastapi_audit.remediate import RECIPES, RemediationCase, RemediationError, RunOptions, recipe_for, run_remediation  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ALLOWED_COMMANDS, ExecutionPolicy, Workspace, command_allowed, digest_tree, run_command  # noqa: E402
from fastapi_audit.remediate.model import STATE_AUDIT_CLEAN, STATE_BLOCKED, STATE_PATCHED, STATE_REJECTED, STATE_VERIFIED, STATES  # noqa: E402
from fastapi_audit.remediate.patches import Blocked, apply_edits, propose  # noqa: E402
from fastapi_audit.remediate.render import render_markdown, render_text  # noqa: E402

FIXTURE = FIXTURES / "remediation19"
REMEDIATE_SHIM = REPO_ROOT / "scripts" / "audit_remediate.py"
UNITTEST_CMD = f"{sys.executable} -m unittest discover -s tests -t . -q"


def _digest(root: Path) -> dict:
    return digest_tree(str(root))


def _policy(command: str = UNITTEST_CMD, **kw) -> ExecutionPolicy:
    return ExecutionPolicy(authorized=True, command=command, timeout_s=kw.pop("timeout_s", 180.0), **kw)


# --------------------------------------------------------------------------- #
# the vertical slice on the fixture
# --------------------------------------------------------------------------- #


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.before = _digest(FIXTURE)
        cls.static = run_remediation(str(FIXTURE), RunOptions())
        cls.verified = run_remediation(str(FIXTURE), RunOptions(policy=_policy()))

    def test_the_fixture_reproduces_its_defects_and_is_never_modified(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], cwd=str(FIXTURE), capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)  # three defect tests fail on the defective service
        self.assertEqual(_digest(FIXTURE), self.before)
        self.assertTrue(self.static["original_untouched"])
        self.assertTrue(self.verified["original_untouched"])

    def test_without_authorization_nothing_runs_and_the_best_state_is_audit_clean(self) -> None:
        run = self.static
        self.assertFalse(run["authorization"]["execution"])
        self.assertFalse(run["tests"]["before"]["ran"])
        self.assertFalse(run["tests"]["after"]["ran"])
        self.assertIsNone(run["defect_observed_before_patch"])
        self.assertEqual(run["summary"]["by_state"], {STATE_AUDIT_CLEAN: 3})
        self.assertEqual(run["summary"]["selected_rules"], ["SA_ASYNC_SESSION_NOT_AWAITED", "TX_COMMIT_INSIDE_BEGIN_BLOCK", "TX_COMMIT_IN_LOOP"])
        for case in run["cases"]:
            self.assertFalse(case["applied_to_project"])
            self.assertTrue(case["verification"]["before_audit"]["target_present"])
            self.assertFalse(case["verification"]["after_audit"]["target_present"])
            self.assertEqual(case["verification"]["after_audit"]["new_findings"], [])
            self.assertIn("tests not executed", case["reason"])

    def test_with_authorization_the_defects_are_observed_then_verified(self) -> None:
        run = self.verified
        self.assertTrue(run["tests"]["before"]["ran"] and run["tests"]["before"]["passed"] is False)
        self.assertTrue(run["tests"]["after"]["ran"] and run["tests"]["after"]["passed"] is True)
        self.assertTrue(run["defect_observed_before_patch"])
        self.assertEqual(run["summary"]["by_state"], {STATE_VERIFIED: 3})
        recipes = {c["rule"]: c["recipe"] for c in run["cases"]}
        self.assertEqual(recipes, {"TX_COMMIT_IN_LOOP": "move_commit_after_loop", "TX_COMMIT_INSIDE_BEGIN_BLOCK": "remove_commit_inside_begin", "SA_ASYNC_SESSION_NOT_AWAITED": "add_await"})
        for case in run["cases"]:
            self.assertEqual([h["to"] for h in case["history"]], [STATE_PATCHED, STATE_VERIFIED])
            self.assertTrue(case["patch"]["diff"].startswith("--- a/shop/service.py\n+++ b/shop/service.py\n"))

    def test_patches_apply_cleanly_to_the_original_and_reproduce_the_verified_copy(self) -> None:
        source = (FIXTURE / "shop" / "service.py").read_text(encoding="utf-8")
        # apply the three proposals in the pipeline's order (highest line first) on the untouched source
        patched = source
        for case in sorted(self.verified["cases"], key=lambda c: -c["line"]):
            from fastapi_audit.remediate.model import Edit

            edits = [Edit(e["line"], e["kind"], e["text"]) for e in case["patch"]["edits"]]
            self.assertEqual(hashlib.sha256(patched.encode()).hexdigest(), case["patch"]["original_sha256"])
            patched = apply_edits(patched, edits)
            self.assertEqual(hashlib.sha256(patched.encode()).hexdigest(), case["patch"]["patched_sha256"])
        self.assertIn("    for item in items:\n        session.add(item)\n    await session.commit()\n", patched)
        self.assertIn("        session.add(item)\n        session.add(adjustment)\n", patched)
        self.assertIn("    await session.execute(message)\n", patched)
        # git recognises the diffs
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, Path(tmp) / "p")
            for case in sorted(self.verified["cases"], key=lambda c: -c["line"]):
                proc = subprocess.run(["git", "apply", "--check", "-"], cwd=str(Path(tmp) / "p"), input=case["patch"]["diff"], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                subprocess.run(["git", "apply", "-"], cwd=str(Path(tmp) / "p"), input=case["patch"]["diff"], capture_output=True, text=True, check=True)
            self.assertEqual((Path(tmp) / "p" / "shop" / "service.py").read_text(encoding="utf-8"), patched)
            proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], cwd=str(Path(tmp) / "p"), capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])

    def test_the_audit_report_names_the_recipe(self) -> None:
        report = audit_fixture("remediation19")
        blocks = {f["rule"]: f["remediation"] for f in report["findings"] if f["remediation"]}
        self.assertEqual({r: (b["recipe"], b["automatable"], b["tool"]) for r, b in blocks.items()}, {
            "TX_COMMIT_IN_LOOP": ("move_commit_after_loop", True, "scripts/audit_remediate.py"),
            "TX_COMMIT_INSIDE_BEGIN_BLOCK": ("remove_commit_inside_begin", True, "scripts/audit_remediate.py"),
            "SA_ASYNC_SESSION_NOT_AWAITED": ("add_await", True, "scripts/audit_remediate.py"),
        })
        self.assertEqual(report["schema_version"], "1.7")

    def test_renderers_and_selection_by_fingerprint(self) -> None:
        text = render_text(self.verified)
        self.assertIn("cases: 3  verified 3", text)
        self.assertIn("defect observed before the patch: yes", text)
        self.assertIn("```diff", render_markdown(self.verified))
        target = self.static["cases"][0]["fingerprint"]
        one = run_remediation(str(FIXTURE), RunOptions(fingerprints={target}))
        self.assertEqual([c["fingerprint"] for c in one["cases"]], [target])


# --------------------------------------------------------------------------- #
# negative and adversarial controls
# --------------------------------------------------------------------------- #


class RejectionTests(ProjectCase):
    def copy_fixture(self) -> None:
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)

    def test_tests_failing_after_the_patch_reject_every_patched_case(self) -> None:
        self.copy_fixture()
        run = run_remediation(str(self.root), RunOptions(policy=_policy(f"{sys.executable} -c \"import sys; sys.exit(3)\"")))
        self.assertEqual(run["summary"]["by_state"], {STATE_REJECTED: 3})
        self.assertTrue(all("tests failed" in c["reason"] for c in run["cases"]))
        self.assertEqual(run["tests"]["after"]["returncode"], 3)

    def test_a_refused_command_leaves_cases_audit_clean_and_says_why(self) -> None:
        self.copy_fixture()
        run = run_remediation(str(self.root), RunOptions(policy=_policy("bash -c 'echo hi'")))
        self.assertFalse(run["tests"]["before"]["ran"])
        self.assertIn("not in the allowed list", run["tests"]["before"]["refused"])
        self.assertEqual(run["summary"]["by_state"], {STATE_AUDIT_CLEAN: 3})
        self.assertTrue(all("not executed" in c["reason"] for c in run["cases"]))

    def test_a_finding_whose_file_differs_in_the_copy_is_rejected_not_patched(self) -> None:
        self.copy_fixture()
        # the project is audited, then the copy is made: simulate a file that the copy cannot reproduce by
        # excluding its directory from the copy through the ignore list
        from fastapi_audit.remediate import pipeline as pl

        original = pl.Workspace.create

        def tampering_create(project_root, workspace_dir=None, ignored=pl.Workspace.ignored if hasattr(pl.Workspace, "ignored") else ()):
            ws = original(project_root, workspace_dir)
            target = ws.project / "shop" / "service.py"
            target.write_text(target.read_text(encoding="utf-8").replace("await session.commit()\n\n\nasync def reprice", "await session.commit()\n    return None\n\n\nasync def reprice"), encoding="utf-8")
            return ws

        pl.Workspace.create = staticmethod(tampering_create)  # type: ignore[assignment]
        try:
            run = run_remediation(str(self.root), RunOptions())
        finally:
            pl.Workspace.create = original  # type: ignore[assignment]
        states = {c["rule"]: c["state"] for c in run["cases"]}
        self.assertIn(STATE_REJECTED, states.values())  # at least the findings of the tampered file (fingerprints moved or the file differs)
        self.assertTrue(run["original_untouched"])

    def test_a_patch_that_still_leaves_the_finding_is_rejected(self) -> None:
        self.copy_fixture()
        from fastapi_audit.remediate import pipeline as pl

        real_apply = pl._apply
        pl._apply = lambda source, proposal: source  # a recipe whose edits change nothing: the finding stays
        try:
            run = run_remediation(str(self.root), RunOptions(select_rules={"SA_ASYNC_SESSION_NOT_AWAITED"}))
        finally:
            pl._apply = real_apply
        self.assertEqual([c["state"] for c in run["cases"]], [STATE_REJECTED])
        self.assertIn("still detected", run["cases"][0]["reason"])

    def test_findings_in_test_code_are_skipped_unless_asked(self) -> None:
        self.copy_fixture()
        self.write("tests/test_helper.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession

            async def helper(session: AsyncSession) -> None:
                session.execute("select 1")
        ''')
        run = run_remediation(str(self.root), RunOptions(select_rules={"SA_ASYNC_SESSION_NOT_AWAITED"}))
        self.assertEqual([c["file"] for c in run["cases"]], ["shop/service.py"])
        run = run_remediation(str(self.root), RunOptions(select_rules={"SA_ASYNC_SESSION_NOT_AWAITED"}, include_tests=True))
        self.assertEqual(sorted(c["file"] for c in run["cases"]), ["shop/service.py", "tests/test_helper.py"])

    def test_the_run_fails_loudly_if_the_project_changes_underneath(self) -> None:
        self.copy_fixture()
        from fastapi_audit.remediate import pipeline as pl

        real = pl.digest_tree
        calls = {"n": 0}

        def flaky(root, *a, **k):
            calls["n"] += 1
            out = real(root, *a, **k)
            if calls["n"] == 3 and Path(root).resolve() == self.root:  # the final digest of the project differs
                out = dict(out, **{"shop/service.py": "0" * 64})
            return out

        pl.digest_tree = flaky
        try:
            with self.assertRaises(RemediationError):
                run_remediation(str(self.root), RunOptions())
        finally:
            pl.digest_tree = real


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
        proposal = propose(f, src)
        self.assertEqual(apply_edits(src, proposal.edits), "async def f(session, items):\n    for item in items:\n        session.add(item)\n    await session.commit()\n")
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


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace.create(str(FIXTURE))
        self.addCleanup(self.ws.cleanup)

    def test_the_copy_excludes_git_and_environments_and_has_a_fresh_home(self) -> None:
        self.assertTrue((self.ws.project / "shop" / "service.py").is_file())
        self.assertFalse((self.ws.project / ".git").exists())
        self.assertTrue(self.ws.home.is_dir())
        self.assertFalse(any(self.ws.home.iterdir()))

    def test_nothing_runs_without_authorization_or_with_a_refused_command(self) -> None:
        self.assertFalse(run_command(ExecutionPolicy(authorized=False, command=UNITTEST_CMD), self.ws).ran)
        self.assertIsNone(command_allowed([sys.executable, "-m", "pytest"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["python3.13", "-m", "unittest"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["uv", "run", "pytest"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIn("not in the allowed list", command_allowed(["bash", "-c", "true"], DEFAULT_ALLOWED_COMMANDS))
        self.assertIn("not in the allowed list", command_allowed(["rm", "-rf", "x"], DEFAULT_ALLOWED_COMMANDS))
        for argv in (["pytest;rm", "-q"], ["python3&&id", "-c", "x"], ["$(id)", "-c", "x"]):
            self.assertIn("shell metacharacters", command_allowed(argv, DEFAULT_ALLOWED_COMMANDS))
        self.assertIsNone(command_allowed(["python3", "-c", "import os; os.getcwd()"], DEFAULT_ALLOWED_COMMANDS))  # no shell: ';' is just text inside one argument
        self.assertIsNone(command_allowed(["make", "test"], DEFAULT_ALLOWED_COMMANDS + ("make",)))  # explicit extension

    def test_the_environment_is_sanitised_and_the_cwd_is_the_copy(self) -> None:
        os.environ["FA_SECRET_PROBE"] = "leaked"
        try:
            run = run_command(_policy(f"{sys.executable} -c \"import os, pathlib; print(os.environ.get('FA_SECRET_PROBE')); print(os.environ['HOME']); print(pathlib.Path.cwd())\""), self.ws)
        finally:
            del os.environ["FA_SECRET_PROBE"]
        self.assertTrue(run.ran and run.passed)
        probe, home, cwd = run.stdout_tail.strip().splitlines()
        self.assertEqual(probe, "None")
        self.assertEqual(Path(home).resolve(), self.ws.home.resolve())
        self.assertEqual(Path(cwd).resolve(), self.ws.project.resolve())

    def test_timeouts_are_enforced_and_reported(self) -> None:
        run = run_command(_policy(f"{sys.executable} -c \"import time; time.sleep(30)\"", timeout_s=1.0), self.ws)
        self.assertTrue(run.ran and run.timed_out and run.passed is False)

    def test_workspace_refuses_unsafe_locations(self) -> None:
        with self.assertRaises(RemediationError):
            Workspace.create(str(FIXTURE), workspace_dir=str(FIXTURE / "inside"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "occupied").write_text("x")
            with self.assertRaises(RemediationError):
                Workspace.create(str(FIXTURE), workspace_dir=tmp)


class ModelTests(unittest.TestCase):
    def test_transitions_are_enforced_and_recorded(self) -> None:
        case = RemediationCase(fingerprint="f", rule="TX_COMMIT_IN_LOOP", file="m.py", line=1, function="f", recipe="move_commit_after_loop")
        case.transition(STATE_PATCHED, "applied")
        case.transition(STATE_AUDIT_CLEAN, "clean")
        case.transition(STATE_VERIFIED, "tests passed")
        self.assertEqual([h["to"] for h in case.history], [STATE_PATCHED, STATE_AUDIT_CLEAN, STATE_VERIFIED])
        with self.assertRaises(RemediationError):
            case.transition(STATE_PATCHED)
        fresh = RemediationCase(fingerprint="g", rule="X", file="m.py", line=1, function=None, recipe=None)
        with self.assertRaises(RemediationError):
            fresh.transition(STATE_VERIFIED)  # verified is never reachable without a patch
        with self.assertRaises(RemediationError):
            fresh.transition("done")
        self.assertEqual(set(STATES), {"proposed", "blocked", "patched", "audit_clean", "verified", "rejected"})
        self.assertFalse(fresh.to_dict()["applied_to_project"])


@unittest.skipUnless(os.environ.get("FA_RUN_PG_REMEDIATION") == "1" and shutil.which("uv") and shutil.which("initdb"), "set FA_RUN_PG_REMEDIATION=1 with uv and PostgreSQL binaries on PATH")
class PostgresRemediationTests(unittest.TestCase):
    """The begin-block recipe end to end on tenant-ledger, inside the isolated copy, on a throwaway PostgreSQL
    cluster. Opt-in because it syncs a virtual environment in the copy (about a minute). The recorded run is in
    examples/tenant-ledger/VERIFICATION.md."""

    def test_begin_block_recipe_is_verified_on_postgresql(self) -> None:
        example = REPO_ROOT / "plugins" / "fastapi-architect" / "examples" / "tenant-ledger"
        passthrough = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")
        run = run_remediation(str(example), RunOptions(select_rules={"TX_COMMIT_INSIDE_BEGIN_BLOCK"}, policy=ExecutionPolicy(
            authorized=True, command="uv run --frozen --extra dev pytest -q -m remediation tests/test_remediation_targets.py", timeout_s=900.0,
            env_passthrough=("PATH", "LANG", "LC_ALL", "TMPDIR") + passthrough)))
        self.assertTrue(run["original_untouched"])
        self.assertTrue(run["defect_observed_before_patch"])
        self.assertEqual(run["summary"]["by_state"], {STATE_VERIFIED: 2})


class CliTests(ProjectCase):
    def test_cli_contract(self) -> None:
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)
        proc = self.cli(".", "--format", "json", "--output", "run.json", "--patch-dir", "patches", script=REMEDIATE_SHIM)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = json.loads(proc.stdout)
        self.assertEqual(run["schema"], "remediation_run/1.0")
        self.assertEqual(run["summary"]["by_state"], {"audit_clean": 3})
        self.assertEqual(json.loads((self.root / "run.json").read_text()), run)
        self.assertEqual(len(list((self.root / "patches").glob("*.diff"))), 3)
        text = self.cli(".", "--select", "TX_COMMIT_IN_LOOP", script=REMEDIATE_SHIM)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("execution: not authorized", text.stdout)
        self.assertIn("audit_clean", text.stdout)
        self.assertEqual(self.cli(".", "--test-command", "pytest", script=REMEDIATE_SHIM).returncode, 2)  # command without authorization
        self.assertEqual(self.cli(".", "--select", "ASYNC_BLOCKING_CALL_INDIRECT", script=REMEDIATE_SHIM).returncode, 2)  # no recipe
        self.assertEqual(self.cli(".", "--select", "NOT_A_RULE", script=REMEDIATE_SHIM).returncode, 2)
        gated = self.cli(".", "--authorize-execution", "--test-command", f"{sys.executable} -c \"import sys; sys.exit(1)\"", "--fail-on-rejected", script=REMEDIATE_SHIM)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("rejected 3", gated.stdout)
        verified = self.cli(".", "--authorize-execution", "--test-command", UNITTEST_CMD, "--format", "markdown", script=REMEDIATE_SHIM)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("| verified | 3 |", verified.stdout)
        self.assertEqual(_digest(self.root), _digest(FIXTURE))  # the copy of the fixture we audited is untouched too


if __name__ == "__main__":
    unittest.main()
