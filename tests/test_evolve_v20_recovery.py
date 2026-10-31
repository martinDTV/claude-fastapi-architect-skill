"""Increment 4: the recoverable writer (journal) and the anchored structural transforms.

The writer is exercised with fault injection before, during and after publication and during recovery; the
transforms with hand-written sources that must survive byte-for-byte outside the inserted lines.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_design import journal, transforms  # noqa: E402
from fastapi_design.generator import GenerationError, WriteError  # noqa: E402

HANDMADE = FIXTURES / "adoption20" / "handmade_shop"
INVENTORY = REPO_ROOT / "plugins" / "fastapi-architect" / "examples" / "generated" / "inventory-service"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob("*") if p.is_file() and ".fastapi-architect/ops" not in p.as_posix()}


class JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-journal-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.project = self.tmp / "project"
        shutil.copytree(INVENTORY, self.project, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
        self.before = tree_digest(self.project)
        # a plan that modifies three existing files and creates two
        self.files = {
            "README.md": "# rewritten readme\n",
            "src/inventory_service/main.py": (self.project / "src/inventory_service/main.py").read_text() + "\n# touched by the plan\n",
            "src/inventory_service/api/router.py": (self.project / "src/inventory_service/api/router.py").read_text() + "\n# touched too\n",
            "src/inventory_service/api/new_module.py": "ROUTES = []\n",
            "tests/test_new_module.py": "def test_nothing():\n    assert True\n",
        }
        self.expected = {rel: (sha(self.project / rel) if (self.project / rel).exists() else None) for rel in self.files}

    def assert_original(self) -> None:
        self.assertEqual(tree_digest(self.project), self.before)

    def assert_published(self) -> None:
        after = tree_digest(self.project)
        for rel, content in self.files.items():
            self.assertEqual(after[rel], hashlib.sha256(content.encode()).hexdigest(), rel)
        for rel in self.before:
            if rel not in self.files:
                self.assertEqual(after[rel], self.before[rel], rel)  # nothing else moved


class RecoverableWriterTests(JournalCase):
    def test_a_corrupt_or_incomplete_journal_blocks_plans_and_is_reported_untouched(self) -> None:
        """v2.0 acceptance: an operation directory whose journal is missing, truncated or of another schema is never
        ignored (an interrupted write may hide behind it): plans are blocked, `recover` reports it as needs_attention
        with the staged files kept, and nothing on disk changes."""
        import subprocess

        from fastapi_design import evolution

        ops = self.project / journal.OPS_DIR
        for op_id, body in (("deadbeef00000001", "{truncated"), ("deadbeef00000002", json.dumps({"schema": "somebody-else/1.0", "op_id": "x"})), ("deadbeef00000003", None)):
            (ops / op_id / "new" / "src").mkdir(parents=True)
            (ops / op_id / "new" / "src" / "staged.py").write_text("x = 1\n", encoding="utf-8")
            if body is not None:
                (ops / op_id / journal.JOURNAL).write_text(body, encoding="utf-8")
        found = journal.unreadable_operations(self.project)
        self.assertEqual([u["op_id"] for u in found], ["deadbeef00000001", "deadbeef00000002", "deadbeef00000003"])
        self.assertEqual([u["reason"].split(" (")[0] for u in found], ["journal.json is unreadable", "journal.json has schema 'somebody-else/1.0' or no op_id", "journal.json is missing"])
        self.assertEqual({u["staged_files"] for u in found}, {"1"})
        self.assertEqual(journal.pending_operations(self.project), [])  # the readable set is empty: this is exactly the gap
        contract = self.tmp / "evolve.json"
        contract.write_text(json.dumps({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(self.project)}, "add_modules": [{"name": "suppliers", "entities": [{"name": "Supplier", "fields": [{"name": "name", "type": "str", "max_length": 80}]}]}]}), encoding="utf-8")
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract)))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("no_unreadable_operation" in b and "deadbeef00000001" in b for b in plan.blockers), plan.blockers)
        cli = REPO_ROOT / "scripts" / "evolve_fastapi.py"
        proc = subprocess.run([sys.executable, str(cli), "recover", str(self.project)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("needs_attention", proc.stdout)
        self.assertIn("inspect by hand, nothing was touched", proc.stdout)
        self.assert_original()  # project files untouched
        for op_id in ("deadbeef00000001", "deadbeef00000002", "deadbeef00000003"):
            self.assertTrue((ops / op_id / "new" / "src" / "staged.py").is_file())  # staged content kept for inspection
        proc = subprocess.run([sys.executable, str(cli), "recover", str(self.project), "--format", "json"], capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertEqual([u["op_id"] for u in payload["unreadable"]], ["deadbeef00000001", "deadbeef00000002", "deadbeef00000003"])
        self.assertEqual(payload["recovered"], [])

    def test_prepare_publishes_nothing_and_stages_everything(self) -> None:
        op = journal.prepare(self.project, self.files, self.expected, kind="test", description="stage only")
        self.assertEqual(op.state, "prepared")
        self.assert_original()
        staged = self.project / op.directory
        self.assertTrue((staged / "journal.json").is_file())
        self.assertEqual(sorted(p.relative_to(staged / "new").as_posix() for p in (staged / "new").rglob("*") if p.is_file()), sorted(self.files))
        self.assertTrue((staged / "backup" / "README.md").is_file())
        self.assertFalse((staged / "backup" / "tests" / "test_new_module.py").exists())  # nothing to back up for a new file
        self.assertEqual([o.op_id for o in journal.pending_operations(self.project)], [op.op_id])
        # the operation id is a function of the plan, not of time
        self.assertEqual(op.op_id, journal.operation_id(self.files, self.expected, "test", "stage only"))

    def test_full_publication_leaves_no_journal(self) -> None:
        op = journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="all")
        self.assert_published()
        self.assertEqual(journal.pending_operations(self.project), [])
        self.assertFalse((self.project / ".fastapi-architect" / "ops").exists())
        self.assertEqual(op.state, "published")

    def test_precondition_failure_before_publication_changes_nothing(self) -> None:
        (self.project / "README.md").write_text("changed meanwhile\n")
        with self.assertRaises(WriteError) as ctx:
            journal.prepare(self.project, self.files, self.expected, kind="test", description="stale")
        self.assertIn("concurrent change", str(ctx.exception))
        self.assertEqual(journal.pending_operations(self.project), [])
        self.assertFalse((self.project / "src/inventory_service/api/new_module.py").exists())

    def test_concurrent_change_between_prepare_and_publish_aborts_before_any_write(self) -> None:
        op = journal.prepare(self.project, self.files, self.expected, kind="test", description="race")
        (self.project / "src/inventory_service/main.py").write_text("# someone else\n")
        with self.assertRaises(WriteError):
            journal.publish(self.project, op)
        self.assertFalse((self.project / "tests/test_new_module.py").exists())
        self.assertEqual((self.project / "README.md").read_text(), (INVENTORY / "README.md").read_text())
        self.assertEqual(journal.load_operation(self.project, op.op_id).state, "prepared")
        report = journal.recover(self.project, op)  # a prepared operation is simply discarded
        self.assertEqual(report.state, "discarded")
        self.assertEqual(journal.pending_operations(self.project), [])

    def test_failure_after_two_files_is_rolled_back_or_rolled_forward(self) -> None:
        for strategy in ("rollback", "forward"):
            with self.subTest(strategy=strategy):
                shutil.rmtree(self.project)
                shutil.copytree(INVENTORY, self.project, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
                faults = journal.FaultInjector(points=["after_publish:2"])
                with self.assertRaises(journal.InjectedFailure):
                    journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="mid", faults=faults)
                pending = journal.pending_operations(self.project)
                self.assertEqual(len(pending), 1)
                op = pending[0]
                self.assertEqual(op.state, "publishing")
                self.assertEqual(len(op.published), 2)  # exactly two files landed
                status = journal.classify(self.project, op)
                self.assertEqual(sorted(k for k, v in status.items() if v == "published"), sorted(op.published))
                self.assertEqual(sorted(k for k, v in status.items() if v == "pending"), sorted(set(self.files) - set(op.published)))
                report = journal.recover(self.project, op, strategy=strategy)
                self.assertEqual(report.conflicts, {})
                if strategy == "rollback":
                    self.assertEqual(report.state, "rolled_back")
                    self.assertEqual(sorted(report.restored), sorted(op.published))
                    self.assert_original()
                else:
                    self.assertEqual(report.state, "completed")
                    self.assertEqual(sorted(report.published), sorted(set(self.files) - set(op.published)))
                    self.assert_published()
                self.assertEqual(journal.pending_operations(self.project), [])

    def test_failure_after_everything_landed_only_needs_cleanup(self) -> None:
        faults = journal.FaultInjector(points=["before_cleanup"])
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="late", faults=faults)
        op = journal.pending_operations(self.project)[0]
        self.assertEqual(op.state, "published")
        report = journal.recover(self.project, op, strategy="forward")
        self.assertEqual(report.state, "completed")
        self.assertEqual(report.published, [])
        self.assertEqual(len(report.already_done), len(self.files))
        self.assert_published()

    def test_interrupted_recovery_can_be_resumed_and_is_idempotent(self) -> None:
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="mid", faults=journal.FaultInjector(points=["after_publish:3"]))
        op = journal.pending_operations(self.project)[0]
        with self.assertRaises(journal.InjectedFailure):
            journal.recover(self.project, op, strategy="rollback", faults=journal.FaultInjector(points=["recover_after:1"]))
        op = journal.pending_operations(self.project)[0]
        self.assertEqual(op.state, "recovering_rollback")
        self.assertEqual(len(op.recovered), 1)
        report = journal.recover(self.project, op, strategy="rollback")
        self.assertEqual(report.state, "rolled_back")
        self.assert_original()
        self.assertEqual(journal.pending_operations(self.project), [])

    def test_concurrent_change_during_recovery_is_reported_and_left_alone(self) -> None:
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="mid", faults=journal.FaultInjector(points=["after_publish:2"]))
        op = journal.pending_operations(self.project)[0]
        victim = op.published[0]
        (self.project / victim).write_text("# edited by a human after the crash\n")
        report = journal.recover(self.project, op, strategy="rollback")
        self.assertEqual(report.state, "needs_attention")
        self.assertEqual(list(report.conflicts), [victim])
        self.assertEqual((self.project / victim).read_text(), "# edited by a human after the crash\n")  # never touched
        other = [p for p in op.published if p != victim][0]
        if self.expected[other] is None:
            self.assertFalse((self.project / other).exists())  # a file this operation created was removed again
        else:
            self.assertEqual(sha(self.project / other), self.before[other])  # the other published file was restored
        self.assertEqual(journal.pending_operations(self.project)[0].state, "needs_attention")

    def test_retry_of_a_completed_operation_is_recognised_by_its_identity(self) -> None:
        op = journal.write_recoverable(self.project, self.files, self.expected, kind="test", description="all")
        new_expected = {rel: hashlib.sha256(content.encode()).hexdigest() for rel, content in self.files.items()}
        again = journal.operation_id(self.files, new_expected, "test", "all")
        self.assertNotEqual(again, op.op_id)  # a retry after completion sees different preconditions: it is a different (no-op) plan
        with self.assertRaises(WriteError):
            journal.prepare(self.project, self.files, self.expected, kind="test", description="all")  # the old preconditions no longer hold

    def test_unsafe_paths_and_symlinks_are_refused_before_staging(self) -> None:
        for bad in ("../outside.py", "/abs.py", "a/../../b.py"):
            with self.assertRaises(GenerationError):
                journal.prepare(self.project, {bad: "x"}, {bad: None}, kind="test", description="bad")
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.project / "linked")
        with self.assertRaises(GenerationError):
            journal.prepare(self.project, {"linked/x.py": "x"}, {"linked/x.py": None}, kind="test", description="bad")
        self.assertFalse((outside / "x.py").exists())
        self.assertEqual(journal.pending_operations(self.project), [])

    def test_write_error_during_staging_leaves_nothing_behind(self) -> None:
        ops = self.project / ".fastapi-architect" / "ops"
        ops.mkdir(parents=True)
        mode = ops.stat().st_mode
        ops.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(ops.chmod, mode)
        if os.access(ops, os.W_OK):
            self.skipTest("the directory stays writable for this user")
        with self.assertRaises(WriteError):
            journal.prepare(self.project, self.files, self.expected, kind="test", description="eio")
        ops.chmod(mode)
        self.assert_original()
        self.assertEqual(journal.pending_operations(self.project), [])


class TransformTests(unittest.TestCase):
    ITEMS = (HANDMADE / "app" / "routers" / "items.py").read_text(encoding="utf-8")
    SCHEMAS = (HANDMADE / "app" / "schemas.py").read_text(encoding="utf-8")

    def test_insert_function_appends_and_preserves_everything_else(self) -> None:
        code = '@router.get("/stats")\nasync def items_stats() -> dict:\n    return {"module": "items"}\n'
        edit = transforms.insert_function("app/routers/items.py", self.ITEMS, code, name="items_stats", router="router", method="GET", route_path="/stats")
        self.assertTrue(edit.source.startswith(self.ITEMS.rstrip("\n")))  # the original text is a prefix, byte for byte
        self.assertIn("# explicit 404", edit.source)
        self.assertIn("# sync on purpose", edit.source)
        self.assertEqual(edit.inserted, ["items_stats"])
        self.assertTrue(edit.evidence["existing_nodes_unchanged"])
        self.assertEqual(edit.evidence["top_level_after"], edit.evidence["top_level_before"] + 1)
        self.assertIn("+async def items_stats", edit.diff)

    def test_insert_function_refuses_duplicates_and_ambiguity(self) -> None:
        code = '@router.get("/x")\nasync def list_items() -> dict:\n    return {}\n'
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_function("items.py", self.ITEMS, code, name="list_items", router="router")
        self.assertIn("duplicate symbol", str(ctx.exception))
        code = '@router.get("/{item_id}")\nasync def other() -> dict:\n    return {}\n'
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_function("items.py", self.ITEMS, code, name="other", router="router", method="GET", route_path="/{item_id}")
        self.assertIn("duplicate endpoint", str(ctx.exception))
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_function("items.py", self.ITEMS, code, name="other", router="nope")
        self.assertIn("not bound", str(ctx.exception))
        twice = self.ITEMS + "\nrouter = APIRouter()\n"
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_function("items.py", twice, code, name="other", router="router")
        self.assertIn("ambiguous", str(ctx.exception))
        with self.assertRaises(transforms.TransformError):
            transforms.insert_function("items.py", self.ITEMS, "x = 1\n", name="other")  # not a single function

    def test_insert_class_field_appends_at_the_body_end(self) -> None:
        edit = transforms.insert_class_field("app/schemas.py", self.SCHEMAS, "ItemIn", "sku: str | None = None", field_name="sku")
        lines = edit.source.splitlines()
        idx = lines.index("    sku: str | None = None")
        self.assertIn("# euros, two decimals by convention", lines[idx - 1])  # placed right after the last field, comment kept
        self.assertIn('"""Pydantic schemas.  NOTE: the odd spacing below is intentional (hand-written style)."""', edit.source)
        self.assertEqual(edit.inserted, ["ItemIn.sku"])
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_class_field("s.py", edit.source, "ItemIn", "sku: str | None = None", field_name="sku")
        self.assertIn("duplicate field", str(ctx.exception))
        with self.assertRaises(transforms.TransformError):
            transforms.insert_class_field("s.py", self.SCHEMAS, "Missing", "x: int = 0", field_name="x")
        ambiguous = self.SCHEMAS + "\n\nclass ItemIn(BaseModel):\n    other: int = 0\n"
        with self.assertRaises(transforms.TransformError) as ctx:
            transforms.insert_class_field("s.py", ambiguous, "ItemIn", "x: int = 0", field_name="x")
        self.assertIn("ambiguous", str(ctx.exception))

    def test_ensure_import_is_idempotent_and_anchored_after_the_imports(self) -> None:
        edit = transforms.ensure_import("items.py", self.ITEMS, "fastapi", ["Query"])
        self.assertIn("from fastapi import Query\n", edit.source)
        head = edit.source.split("router = APIRouter")[0]
        self.assertLess(head.index("from app.schemas import ItemIn, ItemOut"), head.index("from fastapi import Query"))
        again = transforms.ensure_import("items.py", edit.source, "fastapi", ["Query"])
        self.assertEqual(again.source, edit.source)
        self.assertTrue(again.evidence.get("import_already_present"))
        present = transforms.ensure_import("items.py", self.ITEMS, "fastapi", ["APIRouter"])
        self.assertEqual(present.source, self.ITEMS)

    def test_compose_reports_every_step_and_the_result_parses(self) -> None:
        code = '@router.get("/stats")\nasync def items_stats() -> dict:\n    return {"ok": True}\n'
        edit = transforms.compose("items.py", self.ITEMS, [lambda p, s: transforms.ensure_import(p, s, "fastapi", ["Query"]), lambda p, s: transforms.insert_function(p, s, code, name="items_stats", router="router", method="GET", route_path="/stats")])
        self.assertEqual(edit.inserted, ["from fastapi import Query", "items_stats"])
        self.assertEqual(len(edit.evidence["steps"]), 2)
        import ast

        ast.parse(edit.source)

    def test_syntax_errors_in_the_target_or_the_snippet_are_refused(self) -> None:
        with self.assertRaises(transforms.TransformError):
            transforms.insert_function("broken.py", "def broken(:\n    pass\n", "def ok():\n    pass\n", name="ok")
        with self.assertRaises(transforms.TransformError):
            transforms.insert_function("items.py", self.ITEMS, "def ok(:\n    pass\n", name="ok")


if __name__ == "__main__":
    unittest.main()
