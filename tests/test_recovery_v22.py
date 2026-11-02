"""Increment 5 of v2.2: recovery and concurrency hardening of the recoverable writer, the journal and the lock.

The property under test: no interrupted, concurrent or rejected operation silently overwrites somebody else's
change, loses data, or is reported as done without evidence. The fault matrix interrupts the protocol at every
point it exposes (before preparing, after staging some files, after the journal says publishing, before the first
rename, between renames, after the last rename, before and during the cleanup, before and during recovery, while
recovery re-reads the files, on a file fsync, on a directory fsync); every file is classified by digest afterwards;
recovery is resumable, idempotent and never touches a file whose content changed outside the operation. The journal
and the lock are validated against truncation, unknown schemas, duplicates, altered digests, path traversal,
symlinks introduced after the plan, inconsistent lock metadata and leftover temporaries. Scenarios A (generated
project, opt-in merge, interrupted publication), B (adopted project, new module, partial failure, the hand-written
mount file restored) and D (a destructive operation refused with the tree intact) run on the repository's own
fixtures; scenario C and the database side of D run on a real PostgreSQL in ``test_recovery_v22_postgresql.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict
from unittest import mock

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_common.artefacts import walk_files  # noqa: E402
from fastapi_common.fs import TEMP_SUFFIX  # noqa: E402
from fastapi_design import adoption, evolution, journal, locking, pipeline  # noqa: E402
from fastapi_design.generator import GenerationError, WriteError, verify_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
TEAM_NOTES = EXAMPLES / "generated" / "team-notes"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
PARCELS = EXAMPLES / "generated" / "parcels"
ORDERS = EXAMPLES / "generated" / "orders-platform"
WORKSHOP = FIXTURES / "adoption22" / "workshop"
CLI = SKILL_SCRIPTS / "evolve_fastapi.py"
MAIN = "src/team_notes/main.py"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx"))
SCHEMA = evolution.EVOLUTION_SCHEMA
INVARIANTS = ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"]
WORKSHOP_ADOPTION = {"modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}], "package": "app", "unmanaged": ["app/routers/users.py", "app/storage.py"], "mount": {"file": "app/main.py", "function": "create_app", "app": "application", "after_call": "application.include_router", "prefix": "/api"}, "new_modules": {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": "app.main", "attribute": "app"}}, "dependencies": {"allowed": ["app.deps:require_api_key", "app.services:count_items", "app.services:catalogue_value"]}, "operations": ["add_module", "mount_router", "add_endpoint", "add_field"]}
REVIEWS = {"name": "reviews", "entities": [{"name": "Review", "fields": [{"name": "author", "type": "str", "max_length": 80}, {"name": "rating", "type": "int", "min_value": 1}], "operations": ["create", "get", "list"]}]}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-recovery22-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv", ".fastapi-architect/ops"))
        return target

    def write_json(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    # -- a small project of three files, one of them created in a new directory ------------------------------- #

    def small_project(self, name: str = "proj") -> Path:
        project = self.tmp / name
        project.mkdir()
        (project / "a.txt").write_text("a0\n", encoding="utf-8")
        (project / "b.txt").write_text("b0\n", encoding="utf-8")
        (project / "keep.txt").write_text("keep\n", encoding="utf-8")
        return project

    def plan(self, project: Path):
        files = {"a.txt": "a1\n", "b.txt": "b1\n", "pkg/new/c.txt": "c1\n"}
        expected = {"a.txt": sha(project / "a.txt"), "b.txt": sha(project / "b.txt"), "pkg/new/c.txt": None}
        return files, expected

    def assert_old(self, project: Path) -> None:
        self.assertEqual((project / "a.txt").read_text(), "a0\n")
        self.assertEqual((project / "b.txt").read_text(), "b0\n")
        self.assertFalse((project / "pkg").exists(), "the directory created for the new file is removed with it")
        self.assertEqual((project / "keep.txt").read_text(), "keep\n")

    def assert_new(self, project: Path) -> None:
        self.assertEqual((project / "a.txt").read_text(), "a1\n")
        self.assertEqual((project / "b.txt").read_text(), "b1\n")
        self.assertEqual((project / "pkg" / "new" / "c.txt").read_text(), "c1\n")
        self.assertEqual((project / "keep.txt").read_text(), "keep\n")

    def assert_clean(self, project: Path) -> None:
        self.assertEqual(journal.pending_operations(project), [])
        self.assertEqual(journal.unreadable_operations(project), [])
        self.assertEqual(journal.residual_temporaries(project), [])
        self.assertIsNone(locking.inspect(project))
        self.assertFalse((project / journal.OPS_DIR).exists())


# --------------------------------------------------------------------------- #
# the fault matrix
# --------------------------------------------------------------------------- #


class FaultMatrixTests(Case):
    def interrupted(self, project: Path, point: str) -> journal.FaultInjector:
        files, expected = self.plan(project)
        faults = journal.FaultInjector(points=[point])
        with self.assertRaises((journal.InjectedFailure, WriteError)):
            journal.write_recoverable(project, files, expected, kind="test", description="matrix", faults=faults)
        self.assertEqual(faults.hits, [point], "the fault fired exactly once")
        self.assertIsNone(locking.inspect(project), "the lock is released on failure")
        return faults

    def test_before_prepare_and_during_staging_leave_nothing_behind(self) -> None:
        for point in ("before_prepare", "after_stage:1", "after_stage:2"):
            project = self.small_project(point.replace(":", "_"))
            self.interrupted(project, point)
            self.assert_old(project)
            self.assertFalse((project / journal.OPS_DIR).exists(), point)
            self.assert_clean(project)

    def test_after_the_journal_says_publishing_nothing_is_renamed_and_both_strategies_end_clean(self) -> None:
        for strategy, check in (("rollback", self.assert_old), ("forward", self.assert_new)):
            project = self.small_project(strategy)
            self.interrupted(project, "after_journal")
            self.assert_old(project)
            op = journal.pending_operations(project)[0]
            self.assertEqual((op.state, op.published), ("publishing", []))
            self.assertEqual(set(journal.classify(project, op).values()), {"pending"})
            report = journal.recover(project, op, strategy=strategy)
            self.assertEqual(report.state, "rolled_back" if strategy == "rollback" else "completed")
            check(project)
            self.assert_clean(project)

    def test_between_renames_the_tree_is_mixed_until_recovery_classifies_every_file(self) -> None:
        for strategy, check in (("rollback", self.assert_old), ("forward", self.assert_new)):
            for n in (1, 2):
                project = self.small_project(f"{strategy}{n}")
                self.interrupted(project, f"after_publish:{n}")
                op = journal.pending_operations(project)[0]
                status = journal.classify(project, op)
                self.assertEqual(sum(1 for v in status.values() if v == "published"), n)
                self.assertEqual(sum(1 for v in status.values() if v == "pending"), 3 - n)
                self.assertEqual(op.published, [f.path for f in op.files][:n])
                if n >= 1:
                    self.assertEqual((project / "a.txt").read_text(), "a1\n")
                report = journal.recover(project, op, strategy=strategy)
                self.assertEqual(report.state, "rolled_back" if strategy == "rollback" else "completed")
                check(project)
                again = journal.pending_operations(project)
                self.assertEqual(again, [], "idempotent: nothing left to recover")
                self.assert_clean(project)

    def test_after_the_last_rename_only_the_journal_state_is_missing(self) -> None:
        project = self.small_project()
        self.interrupted(project, "after_publish_all")
        self.assert_new(project)
        op = journal.pending_operations(project)[0]
        self.assertEqual(op.state, "publishing")
        self.assertEqual(set(journal.classify(project, op).values()), {"published"})
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual((report.state, report.published, len(report.already_done)), ("completed", [], 3))
        self.assert_new(project)
        self.assert_clean(project)
        # and a rollback from the same point restores the old tree, directory included
        project = self.small_project("rb")
        self.interrupted(project, "after_publish_all")
        report = journal.recover(project, journal.pending_operations(project)[0], strategy="rollback")
        self.assertEqual((report.state, sorted(report.restored), report.removed_dirs), ("rolled_back", ["a.txt", "b.txt", "pkg/new/c.txt"], ["pkg/new", "pkg"]))
        self.assert_old(project)
        self.assert_clean(project)

    def test_an_interrupted_cleanup_is_finished_not_mistaken_for_an_interrupted_publication(self) -> None:
        project = self.small_project()
        self.interrupted(project, "during_cleanup")
        self.assert_new(project)
        raw = json.loads((project / journal.OPS_DIR).iterdir().__next__().joinpath("journal.json").read_text())
        self.assertEqual(raw["state"], "completed")
        self.assertEqual(journal.pending_operations(project), [], "a completed journal is cleaned up by the next reader, not reported")
        self.assert_clean(project)
        # before_cleanup: the journal says published; both strategies agree there is nothing to undo or redo
        project = self.small_project("bc")
        self.interrupted(project, "before_cleanup")
        op = journal.pending_operations(project)[0]
        self.assertEqual(op.state, "published")
        self.assertEqual(journal.recover(project, op, strategy="forward").state, "completed")
        self.assert_new(project)
        self.assert_clean(project)

    def test_an_interrupted_recovery_resumes_with_its_own_strategy_only(self) -> None:
        for strategy, check in (("rollback", self.assert_old), ("forward", self.assert_new)):
            project = self.small_project(strategy)
            self.interrupted(project, "after_publish:2" if strategy == "rollback" else "after_publish:1")
            op = journal.pending_operations(project)[0]
            with self.assertRaises(journal.InjectedFailure):
                journal.recover(project, op, strategy=strategy, faults=journal.FaultInjector(points=["recover_after:1"]))
            op = journal.pending_operations(project)[0]
            self.assertEqual((op.state, op.recovery_strategy), (f"recovering_{strategy}", strategy))
            self.assertGreaterEqual(len(op.recovered), 1)
            other = "forward" if strategy == "rollback" else "rollback"
            with self.assertRaises(WriteError) as ctx:
                journal.recover(project, op, strategy=other)
            self.assertIn("resume with the same strategy", str(ctx.exception))
            self.assertEqual(journal.pending_operations(project)[0].state, f"recovering_{strategy}", "the refused switch changed nothing")
            report = journal.recover(project, op, strategy=strategy)
            self.assertEqual(report.state, "rolled_back" if strategy == "rollback" else "completed")
            check(project)
            self.assert_clean(project)

    def test_a_failure_while_recovery_re_reads_the_files_leaves_a_resumable_journal(self) -> None:
        project = self.small_project()
        self.interrupted(project, "after_publish:2")
        op = journal.pending_operations(project)[0]
        before = tree(project)
        with self.assertRaises(journal.InjectedFailure):
            journal.recover(project, op, strategy="rollback", faults=journal.FaultInjector(points=["during_classify"]))
        self.assertEqual(tree(project), before, "nothing changed while the evidence was being rebuilt")
        op = journal.pending_operations(project)[0]
        self.assertEqual((op.state, op.recovered), ("recovering_rollback", []))
        self.assertEqual(journal.recover(project, op, strategy="rollback").state, "rolled_back")
        self.assert_old(project)
        self.assert_clean(project)

    def test_a_failing_file_fsync_leaves_a_recoverable_journal_and_no_partial_file(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        files["b.txt"] = "b1 with a longer body so its temporary is recognisable by size\n"
        op = journal.prepare(project, files, expected, kind="test", description="fsync")
        real_fsync = os.fsync
        marker = len(files["b.txt"])

        def flaky(fd: int) -> None:  # the fsync of the second file's temporary fails (an EIO from the device)
            if os.fstat(fd).st_size == marker:
                raise OSError(5, "injected EIO on fsync")
            real_fsync(fd)

        with mock.patch("fastapi_common.fs.os.fsync", flaky):
            with self.assertRaises(OSError):
                journal.publish(project, op)
        self.assertEqual(journal.residual_temporaries(project), [], "the temporary of the failed write is removed; the destination keeps the previous content")
        self.assertEqual((project / "b.txt").read_text(), "b0\n")
        pending = journal.pending_operations(project)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].published, ["a.txt"])
        status = journal.classify(project, pending[0])
        self.assertNotIn("conflict", status.values())
        report = journal.recover(project, pending[0], strategy="rollback")
        self.assertEqual(report.state, "rolled_back")
        self.assert_old(project)
        self.assert_clean(project)

    def test_a_directory_sync_that_fails_hard_is_an_error_and_recoverable_while_an_unsupported_one_is_not(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        op = journal.prepare(project, files, expected, kind="test", description="dirsync")
        with mock.patch("fastapi_common.fs.fsync_directory", side_effect=OSError(5, "injected EIO on directory fsync")):
            with self.assertRaises(OSError):
                journal.publish(project, op)
        pending = journal.pending_operations(project)
        self.assertEqual(len(pending), 1)
        state = pending[0].state
        self.assertIn(state, ("publishing", "prepared"))
        self.assertEqual(journal.recover(project, pending[0], strategy="rollback").state, "rolled_back" if state == "publishing" else "discarded")
        self.assert_old(project)
        self.assert_clean(project)
        with mock.patch("fastapi_common.fs.fsync_directory", return_value=False):  # a filesystem that cannot sync directories: reported, not fatal
            op = journal.write_recoverable(project, files, expected, kind="test", description="dirsync2")
        self.assertEqual(op.state, "completed")
        self.assert_new(project)
        self.assert_clean(project)


# --------------------------------------------------------------------------- #
# nothing of somebody else's is overwritten: per-file re-check, guards, conflicts
# --------------------------------------------------------------------------- #


class ChangingWorld(journal.FaultInjector):
    """A fault injector that edits a file at a point instead of failing: the world moves while we publish."""

    def __init__(self, point: str, path: Path, text: str) -> None:
        super().__init__(points=[])
        self.point, self.path, self.text = point, path, text

    def check(self, point: str) -> None:
        if point == self.point:
            self.hits.append(point)
            self.path.write_text(self.text, encoding="utf-8")


class ForeignChangeTests(Case):
    def test_a_file_changed_during_the_publication_is_refused_recorded_and_never_overwritten(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        world = ChangingWorld("after_publish:1", project / "b.txt", "b-by-someone-else\n")
        with self.assertRaises(WriteError) as ctx:
            journal.write_recoverable(project, files, expected, kind="test", description="moving", faults=world)
        self.assertIn("concurrent change during publication: b.txt", str(ctx.exception))
        self.assertEqual((project / "b.txt").read_text(), "b-by-someone-else\n", "the foreign content stays")
        self.assertEqual((project / "a.txt").read_text(), "a1\n")
        self.assertFalse((project / "pkg").exists(), "publication stopped at the refused file")
        op = journal.pending_operations(project)[0]
        self.assertEqual((op.state, op.published), ("publishing", ["a.txt"]))
        self.assertTrue(op.conflicts["b.txt"].startswith(journal.NOT_WRITTEN))
        status = journal.classify(project, op)
        self.assertEqual(status, {"a.txt": "published", "b.txt": "conflict", "pkg/new/c.txt": "pending"})
        # forward cannot finish: the foreign file is a conflict, left alone, needs attention
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual(report.state, "needs_attention")
        self.assertEqual(list(report.conflicts), ["b.txt"])
        self.assertEqual((project / "b.txt").read_text(), "b-by-someone-else\n")
        self.assertEqual((project / "pkg" / "new" / "c.txt").read_text(), "c1\n", "the pending file was published forward")
        op = journal.pending_operations(project)[0]
        self.assertEqual(op.state, "needs_attention")
        # the operator restores the expected content: forward then completes
        (project / "b.txt").write_text("b0\n", encoding="utf-8")
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual((report.state, report.published), ("completed", ["b.txt"]))
        self.assert_new(project)
        self.assert_clean(project)

    def test_a_rollback_skips_the_file_the_publication_refused_because_it_was_never_written(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        world = ChangingWorld("after_publish:1", project / "b.txt", "b-by-someone-else\n")
        with self.assertRaises(WriteError):
            journal.write_recoverable(project, files, expected, kind="test", description="moving", faults=world)
        op = journal.pending_operations(project)[0]
        report = journal.recover(project, op, strategy="rollback")
        self.assertEqual((report.state, report.restored, report.conflicts), ("rolled_back", ["a.txt"], {}))
        self.assertIn("b.txt", report.already_done)
        self.assertEqual((project / "a.txt").read_text(), "a0\n")
        self.assertEqual((project / "b.txt").read_text(), "b-by-someone-else\n", "left exactly as the other writer left it")
        self.assertFalse((project / "pkg").exists())
        self.assert_clean(project)

    def test_a_published_file_changed_again_before_recovery_is_a_conflict_for_both_strategies(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="x", faults=journal.FaultInjector(points=["after_publish:1"]))
        (project / "a.txt").write_text("a-edited-after-the-crash\n", encoding="utf-8")
        op = journal.pending_operations(project)[0]
        for strategy in ("rollback", "forward"):
            report = journal.recover(project, op, strategy=strategy) if strategy == "rollback" else None
            if report is None:
                with self.assertRaises(WriteError):  # the strategy is fixed once recovery started
                    journal.recover(project, op, strategy="forward")
                continue
            self.assertEqual(report.state, "needs_attention")
            self.assertEqual((project / "a.txt").read_text(), "a-edited-after-the-crash\n")
            self.assertEqual((project / "b.txt").read_text(), "b0\n")
        (project / "a.txt").write_text("a1\n", encoding="utf-8")  # the operator puts our version back: the rollback restores it
        report = journal.recover(project, journal.pending_operations(project)[0], strategy="rollback")
        self.assertEqual((report.state, report.restored), ("rolled_back", ["a.txt"]))
        self.assert_old(project)
        self.assert_clean(project)

    def test_guards_hold_files_the_plan_read_but_does_not_write(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        guards = {"keep.txt": sha(project / "keep.txt")}
        (project / "keep.txt").write_text("keep-changed\n", encoding="utf-8")
        with self.assertRaises(WriteError) as ctx:
            journal.write_recoverable(project, files, expected, kind="test", description="g", guards=guards)
        self.assertIn("keep.txt, which the plan read, changed", str(ctx.exception))
        self.assertEqual((project / "a.txt").read_text(), "a0\n")
        self.assertFalse((project / journal.OPS_DIR).exists(), "nothing was staged")
        (project / "keep.txt").write_text("keep\n", encoding="utf-8")
        op = journal.prepare(project, files, expected, kind="test", description="g", guards=guards)
        self.assertEqual(json.loads((project / op.directory / "journal.json").read_text())["guards"], guards)
        (project / "keep.txt").write_text("keep-changed\n", encoding="utf-8")
        with self.assertRaises(WriteError):
            journal.publish(project, op)
        self.assertEqual((project / "a.txt").read_text(), "a0\n", "refused before the first rename")
        self.assertEqual(journal.recover(project, journal.pending_operations(project)[0]).state, "discarded")
        with self.assertRaises(WriteError):
            journal.write_recoverable(project, files, expected, kind="test", description="g", guards={"a.txt": sha(project / "a.txt")})
        self.assert_clean(project)

    def test_a_project_with_a_pending_or_unreadable_operation_is_not_written_again(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="first", faults=journal.FaultInjector(points=["after_publish:1"]))
        with self.assertRaises(WriteError) as ctx:
            journal.write_recoverable(project, {"keep.txt": "other\n"}, {"keep.txt": sha(project / "keep.txt")}, kind="test", description="second")
        self.assertIn("an interrupted operation exists", str(ctx.exception))
        self.assertEqual((project / "keep.txt").read_text(), "keep\n")
        self.assertEqual(len(journal.pending_operations(project)), 1, "the second operation staged nothing")
        journal.recover(project, journal.pending_operations(project)[0], strategy="rollback")
        (project / journal.OPS_DIR / "deadbeefdeadbeef").mkdir(parents=True)
        (project / journal.OPS_DIR / "deadbeefdeadbeef" / "journal.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(WriteError) as ctx:
            journal.write_recoverable(project, {"keep.txt": "other\n"}, {"keep.txt": sha(project / "keep.txt")}, kind="test", description="third")
        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual((project / "keep.txt").read_text(), "keep\n")


# --------------------------------------------------------------------------- #
# journal and lock integrity
# --------------------------------------------------------------------------- #


class JournalIntegrityTests(Case):
    def pending_journal(self, project: Path) -> Path:
        files, expected = self.plan(project)
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="x", faults=journal.FaultInjector(points=["after_publish:1"]))
        op = journal.pending_operations(project)[0]
        return project / op.directory / "journal.json"

    def test_malformed_journals_are_reported_with_a_reason_and_block_without_being_acted_upon(self) -> None:
        project = self.small_project()
        path = self.pending_journal(project)
        good = json.loads(path.read_text())
        before = tree(project)
        cases = {
            "truncated": path.read_text()[:60],
            "unknown schema": json.dumps({**good, "schema": "somebody-else/9.9"}),
            "duplicate path": json.dumps({**good, "files": good["files"] + [good["files"][0]]}),
            "altered digest": json.dumps({**good, "files": [{**good["files"][0], "new": "not-a-digest"}] + good["files"][1:]}),
            "traversal": json.dumps({**good, "files": [{**good["files"][0], "path": "../outside.txt"}] + good["files"][1:]}),
            "inside ops": json.dumps({**good, "files": [{**good["files"][0], "path": ".fastapi-architect/ops/x"}] + good["files"][1:]}),
            "published names a stranger": json.dumps({**good, "published": ["stranger.txt"]}),
            "published repeats": json.dumps({**good, "published": ["a.txt", "a.txt"]}),
            "unknown state": json.dumps({**good, "state": "teleported"}),
            "foreign identity": json.dumps({**good, "op_id": "0123456789abcdef"}),
            "guard is also a file": json.dumps({**good, "guards": {"a.txt": good["files"][0]["expected"]}}),
            "bad created_dirs": json.dumps({**good, "created_dirs": ["../x"]}),
        }
        for label, text in cases.items():
            path.write_text(text, encoding="utf-8")
            self.assertEqual(journal.pending_operations(project), [], label)
            found = journal.unreadable_operations(project)
            self.assertEqual(len(found), 1, label)
            self.assertTrue(found[0]["reason"], label)
            self.assertEqual(tree(project), before, label)
            reason = journal.validate_journal(json.loads(text) if not text.startswith("{\n  \"conf") and label != "truncated" else None, good["op_id"])
            self.assertTrue(reason, label)
        path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(len(journal.pending_operations(project)), 1, "the intact journal is pending again")
        self.assertIsNone(journal.validate_journal(good, good["op_id"]))

    def test_a_tampered_staging_copy_or_backup_is_never_used(self) -> None:
        project = self.small_project()
        path = self.pending_journal(project)
        op_dir = path.parent
        (op_dir / "new" / "b.txt").write_text("tampered\n", encoding="utf-8")
        op = journal.pending_operations(project)[0]
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual(report.state, "needs_attention")
        self.assertIn("does not match the journal", report.conflicts["b.txt"])
        self.assertEqual((project / "b.txt").read_text(), "b0\n", "the tampered copy was not published")
        self.assertEqual((project / "pkg" / "new" / "c.txt").read_text(), "c1\n", "the intact copy was")
        project = self.small_project("rb")
        path = self.pending_journal(project)
        (path.parent / "backup" / "a.txt").write_text("tampered backup\n", encoding="utf-8")
        report = journal.recover(project, journal.pending_operations(project)[0], strategy="rollback")
        self.assertEqual(report.state, "needs_attention")
        self.assertIn("backup", report.conflicts["a.txt"])
        self.assertEqual((project / "a.txt").read_text(), "a1\n", "a backup that does not match the journal never overwrites the file")
        (path.parent / "new" / "b.txt").unlink()
        project2 = self.small_project("missing")
        path2 = self.pending_journal(project2)
        (path2.parent / "new" / "b.txt").unlink()
        report = journal.recover(project2, journal.pending_operations(project2)[0], strategy="forward")
        self.assertEqual(report.state, "needs_attention")
        self.assertIn("missing", report.conflicts["b.txt"])

    def test_a_symlink_introduced_after_the_plan_is_refused_by_publication_and_by_recovery(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        op = journal.prepare(project, files, expected, kind="test", description="s")
        outside = self.tmp / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (project / "b.txt").unlink()
        os.symlink(outside, project / "b.txt")
        with self.assertRaises((GenerationError, WriteError)):
            journal.publish(project, op)
        self.assertEqual(outside.read_text(), "outside\n", "nothing was written through the link")
        self.assertEqual((project / "a.txt").read_text(), "a0\n", "refused before the first rename")
        (project / "b.txt").unlink()
        (project / "b.txt").write_text("b0\n", encoding="utf-8")
        op = journal.pending_operations(project)[0]
        self.assertEqual(op.state, "prepared")
        self.assertEqual(journal.recover(project, op, strategy="rollback").state, "discarded")
        # a link planted between the interruption and the recovery
        project = self.small_project("after")
        self.pending_journal(project)
        (project / "b.txt").unlink()
        os.symlink(outside, project / "b.txt")
        op = journal.pending_operations(project)[0]
        self.assertEqual(journal.classify(project, op)["b.txt"], "unsafe")
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual(report.state, "needs_attention")
        self.assertIn("symlink", report.conflicts["b.txt"])
        self.assertEqual(outside.read_text(), "outside\n")

    def test_parent_operations_identity_replays_and_stray_entries(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        faults = journal.FaultInjector(points=["after_publish:1"])
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="child", faults=faults, steps=[{"kind": "regenerate", "paths": ["a.txt"]}], parent_op="0000000000000000")
        op = journal.pending_operations(project)[0]
        self.assertEqual(op.parent_op, "0000000000000000", "an absent parent is informational: the child recovers on its own")
        with self.assertRaises(WriteError) as ctx:
            journal.prepare(project, files, expected, kind="test", description="child")
        self.assertIn("is already publishing", str(ctx.exception))
        self.assertEqual(journal.recover(project, op, strategy="forward").state, "completed")
        with self.assertRaises(WriteError) as ctx:  # the same identity again: its preconditions no longer hold
            journal.write_recoverable(project, files, expected, kind="test", description="child")
        self.assertIn("concurrent change", str(ctx.exception))
        ops = project / journal.OPS_DIR
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "stray.txt").write_text("?", encoding="utf-8")
        os.symlink(self.tmp, ops / "linked")
        found = {u["op_id"]: u["reason"] for u in journal.unreadable_operations(project)}
        self.assertIn("stray file", found["stray.txt"])
        self.assertIn("symlink", found["linked"])
        self.assertEqual(journal.pending_operations(project), [])

    def test_residual_temporaries_are_listed_never_deleted_and_the_cli_asks_for_attention(self) -> None:
        project = self.small_project()
        (project / ("a.txt" + TEMP_SUFFIX)).write_text("half", encoding="utf-8")
        (project / ".venv").mkdir()
        (project / ".venv" / ("x" + TEMP_SUFFIX)).write_text("ignored", encoding="utf-8")
        self.assertEqual(journal.residual_temporaries(project), ["a.txt" + TEMP_SUFFIX])
        proc = subprocess.run([sys.executable, str(CLI), "ops", "list", str(project)], capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("residual temporary: a.txt" + TEMP_SUFFIX, proc.stdout)
        self.assertTrue((project / ("a.txt" + TEMP_SUFFIX)).exists())
        proc = subprocess.run([sys.executable, str(CLI), "ops", "list", str(project), "--format", "json"], capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(json.loads(proc.stdout)["residual_temporaries"], ["a.txt" + TEMP_SUFFIX])


class LockIntegrityTests(Case):
    def lock_path(self, project: Path) -> Path:
        path = project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_inconsistent_metadata_is_unreadable_and_never_treated_as_free(self) -> None:
        project = self.small_project()
        path = self.lock_path(project)
        for label, record in (("pid is a string", {"schema": locking.LOCK_SCHEMA, "pid": "12", "host": "h", "token": "t"}), ("no host", {"schema": locking.LOCK_SCHEMA, "pid": 12, "token": "t"}), ("boolean pid", {"schema": locking.LOCK_SCHEMA, "pid": True, "host": "h"}), ("empty", ""), ("array", "[]")):
            path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
            state = locking.inspect(project)
            self.assertEqual(state["status"], "unreadable", label)
            with self.assertRaises(locking.LockHeld) as ctx:
                locking.acquire(project, kind="evolve")
            self.assertEqual(ctx.exception.reason, "unreadable", label)
            self.assertTrue(path.exists(), label)
        self.assertTrue(locking.unlock_stale(project)["removed"], "an unreadable lock is removed only on request")
        self.assertIsNone(locking.inspect(project))

    def test_a_lock_that_is_a_symlink_or_a_directory_is_invalid_and_removed_by_hand_only(self) -> None:
        project = self.small_project()
        path = self.lock_path(project)
        os.symlink(self.tmp / "nowhere", path)
        self.assertEqual(locking.inspect(project)["status"], "invalid")
        with self.assertRaises(locking.LockHeld) as ctx:
            locking.acquire(project, kind="evolve", break_stale=True)
        self.assertEqual(ctx.exception.reason, "invalid")
        self.assertFalse(locking.unlock_stale(project)["removed"])
        self.assertTrue(path.is_symlink())
        path.unlink()
        path.mkdir()
        self.assertEqual(locking.inspect(project)["status"], "invalid")
        self.assertFalse(locking.unlock_stale(project)["removed"])
        path.rmdir()
        self.assertIsNone(locking.inspect(project))

    def test_the_lock_record_is_complete_from_its_first_visible_moment_and_a_same_host_dead_pid_is_stale(self) -> None:
        project = self.small_project()
        lock = locking.acquire(project, kind="evolve", description="me")
        data = json.loads(lock.path.read_text())
        self.assertEqual((data["pid"], data["token"]), (os.getpid(), lock.token))
        self.assertEqual([p.name for p in lock.path.parent.iterdir()], [locking.LOCK_FILE], "no temporary is left next to the lock")
        self.assertEqual(locking.inspect(project)["status"], "held")
        # another process broke our lock meanwhile: release says so and never removes the newcomer's lock
        lock.path.unlink()
        other = locking.acquire(project, kind="evolve", description="other")
        self.assertFalse(lock.release())
        self.assertEqual(json.loads(other.path.read_text())["token"], other.token)
        self.assertTrue(other.release())
        path = self.lock_path(project)
        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": 2**22 + 12345, "host": os.uname().nodename if hasattr(os, "uname") else "localhost", "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "dead", "token": "d"}), encoding="utf-8")
        import socket

        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": 2**22 + 12345, "host": socket.gethostname(), "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "dead", "token": "d"}), encoding="utf-8")
        self.assertEqual(locking.inspect(project)["status"], "stale")
        with self.assertRaises(locking.LockHeld) as ctx:
            locking.acquire(project, kind="evolve")
        self.assertEqual(ctx.exception.reason, "stale")
        taken = locking.acquire(project, kind="evolve", break_stale=True)
        self.assertEqual(taken.record["broke"]["description"], "dead")
        taken.release()
        self.assert_clean(project)


# --------------------------------------------------------------------------- #
# generation through the journal
# --------------------------------------------------------------------------- #


class GenerationRecoveryTests(Case):
    def test_an_interrupted_generation_is_journaled_recoverable_and_the_retry_is_identical(self) -> None:
        target = self.tmp / "gen"
        spec = EXAMPLES / "specs" / "inventory-service.json"
        real = pipeline.write_recoverable

        def interrupted(*args, **kwargs):
            kwargs["faults"] = journal.FaultInjector(points=["after_publish:5"])
            return real(*args, **kwargs)

        with mock.patch.object(pipeline, "write_recoverable", interrupted):
            with self.assertRaises(journal.InjectedFailure):
                pipeline.run_design(str(spec), pipeline.DesignOptions(target=str(target), audit=False))
        pending = journal.pending_operations(target)
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0].kind, len(pending[0].published)), ("generate", 5))
        status = journal.classify(target, pending[0])
        self.assertEqual(sum(1 for v in status.values() if v == "published"), 5)
        report = journal.recover(target, pending[0], strategy="rollback")
        self.assertEqual(report.state, "rolled_back")
        self.assertEqual([p for p in walk_files(target)], [], "a rolled-back fresh generation leaves no file behind")
        result = pipeline.run_design(str(spec), pipeline.DesignOptions(target=str(target), audit=False))
        self.assertEqual(result["generation"]["conflicts"], [])
        self.assertTrue(all(v == "identical" for v in verify_manifest(str(target))["status"].values()))
        self.assert_clean(target)
        # a forward recovery of the same interruption yields the same tree as the uninterrupted generation
        other = self.tmp / "gen2"
        with mock.patch.object(pipeline, "write_recoverable", interrupted):
            with self.assertRaises(journal.InjectedFailure):
                pipeline.run_design(str(spec), pipeline.DesignOptions(target=str(other), audit=False))
        self.assertEqual(journal.recover(other, journal.pending_operations(other)[0], strategy="forward").state, "completed")
        self.assertEqual(tree(other), tree(target))

    def test_a_locked_target_is_a_generation_error_not_a_traceback(self) -> None:
        target = self.tmp / "gen"
        target.mkdir()
        lock = locking.acquire(target, kind="evolve", description="someone")
        try:
            with self.assertRaises(GenerationError) as ctx:
                pipeline.run_design(str(EXAMPLES / "specs" / "inventory-service.json"), pipeline.DesignOptions(target=str(target), audit=False, allow_existing=True))
            self.assertIn("locked by", str(ctx.exception))
        finally:
            lock.release()
        self.assertEqual([p for p in walk_files(target)], [])


# --------------------------------------------------------------------------- #
# the operations of the engine: scenarios A, B, D and the report
# --------------------------------------------------------------------------- #


class ScenarioTests(Case):
    def merge_contract(self, project: Path) -> Path:
        return self.write_json("merge.json", {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "reminders", "description": "reminders", "entities": [{"name": "Reminder", "fields": [{"name": "text", "type": "str", "max_length": 80}], "operations": ["create", "get", "list"]}]}], "invariants": INVARIANTS, "files": {"merge": [{"path": MAIN, "transform": "three_way_python"}]}})

    def manual_main(self, project: Path) -> str:
        main = project / MAIN
        text = main.read_text(encoding="utf-8")
        text = text.replace("from fastapi import FastAPI\n", "from fastapi import FastAPI\nfrom starlette.middleware.gzip import GZipMiddleware  # manual: compress large payloads\n")
        text = text.replace("    install_error_handlers(app)\n", "    install_error_handlers(app)\n    app.add_middleware(GZipMiddleware, minimum_size=1024)  # manual: added by the team\n")
        main.write_text(text, encoding="utf-8")
        return text

    def test_a_an_interrupted_merge_publication_is_recovered_and_the_retry_merges_again(self) -> None:
        project = self.copy(TEAM_NOTES, "notes")
        manual = self.manual_main(project)
        before = tree(project)
        contract = self.merge_contract(project)
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, allow_merge=True, faults=journal.FaultInjector(points=["after_publish:3"])))
        self.assertEqual(report["applied"]["state"], "interrupted")
        pending = journal.pending_operations(project)
        self.assertEqual(len(pending), 1)
        self.assertIn("merge", [s["kind"] for s in pending[0].steps])
        status = journal.classify(project, pending[0])
        self.assertEqual(sum(1 for v in status.values() if v == "published"), 3)
        again = evolution.plan_evolution(evolution.load_evolution(str(contract)), allow_merge=True)
        self.assertEqual([b.split(":")[0] for b in again.blockers], ["no_pending_operation"])
        outcome = journal.recover(project, pending[0], strategy="rollback")
        self.assertEqual(outcome.state, "rolled_back")
        self.assertEqual(tree(project), before, "the developer's main.py and every other file are exactly as before")
        self.assertEqual((project / MAIN).read_text(encoding="utf-8"), manual)
        retry = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, allow_merge=True))
        self.assertEqual(retry["applied"]["state"], "applied", retry["plan"]["blockers"])
        merged = (project / MAIN).read_text(encoding="utf-8")
        self.assertIn("GZipMiddleware, minimum_size=1024)  # manual: added by the team", merged)
        self.assertIn("reminders", merged)
        self.assertEqual(retry["applied"]["database"]["migration_required"], False)
        self.assert_clean(project)
        self.assertEqual(evolution.plan_evolution(evolution.load_evolution(str(contract)), allow_merge=True).state, "noop")

    def test_b_a_partial_failure_while_mounting_a_module_restores_the_hand_written_mount_file(self) -> None:
        shop = self.copy(WORKSHOP, "shop")
        contract, errors = adoption.validate_adoption({"schema": adoption.ADOPTION_SCHEMA_11, "project": {"path": str(shop)}, **WORKSHOP_ADOPTION})
        self.assertEqual(errors, [])
        adoption.adopt(contract)
        before = tree(shop)
        evo = self.write_json("reviews.json", {"schema": SCHEMA, "project": {"path": str(shop)}, "add_modules": [REVIEWS]})
        report = evolution.run_evolution(str(evo), evolution.EvolveOptions(apply=True, verify=False, faults=journal.FaultInjector(points=["after_publish:2"])))
        self.assertEqual(report["applied"]["state"], "interrupted")
        pending = journal.pending_operations(shop)[0]
        self.assertEqual(pending.published, [".fastapi-architect/adoption.json", "app/main.py"])
        self.assertIn("reviews_router", (shop / "app" / "main.py").read_text(), "the mount file landed before the crash")
        self.assertEqual([s["kind"] for s in pending.steps], ["generate", "mount", "manifest"])
        # a second evolution and a second adoption are refused while the journal is pending
        other = evolution.run_evolution(str(evo), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual([b.split(":")[0] for b in other["plan"]["blockers"]], ["no_pending_operation"])
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.adopt(contract)
        self.assertIn("interrupted operation", "\n".join(ctx.exception.errors))
        outcome = journal.recover(shop, pending, strategy="rollback")
        self.assertEqual(outcome.state, "rolled_back")
        self.assertEqual(sorted(outcome.restored), [".fastapi-architect/adoption.json", "app/main.py"])
        self.assertEqual(tree(shop), before, "the hand-written mount file and the adoption record are byte-identical again")
        self.assertFalse((shop / "app" / "reviews").exists(), "no half-created package is left behind")
        retry = evolution.run_evolution(str(evo), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(retry["applied"]["state"], "applied", retry["plan"]["blockers"])
        self.assertEqual(retry["applied"]["database"]["status"], "no schema change in this plan")
        self.assert_clean(shop)
        # the guards: a file the plan read (a delegate target) changed between plan and apply refuses the write
        ext = self.write_json("ext.json", {"schema": SCHEMA, "project": {"path": str(shop)}, "modify_modules": [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/reports/value", "name": "items_value", "delegate": {"callable": "app.services:catalogue_value", "arguments": []}}]}]})
        plan = evolution.plan_evolution(evolution.load_evolution(str(ext)))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertIn("app/services.py", plan.guards)
        services = shop / "app" / "services.py"
        services.write_text(services.read_text().replace("async def catalogue_value", "async def catalogue_value_renamed"), encoding="utf-8")
        snapshot = tree(shop)
        with self.assertRaises(WriteError) as ctx:
            evolution._write_plan(shop, plan)
        self.assertIn("app/services.py, which the plan read, changed", str(ctx.exception))
        self.assertEqual(tree(shop), snapshot)
        self.assert_clean(shop)

    def test_d_a_destructive_contract_that_is_refused_leaves_the_tree_intact(self) -> None:
        project = self.copy(PARCELS, "parcels")
        before = tree(project)
        doc = json.loads((EXAMPLES / "evolutions" / "parcels-contract-label.json").read_text())
        doc["project"]["path"] = str(project)
        contract = self.write_json("contract.json", doc)
        for options in (evolution.EvolveOptions(apply=True, verify=False), evolution.EvolveOptions(apply=True, verify=False, authorize_contract=True), evolution.EvolveOptions(apply=True, verify=False, authorize_contract=True, confirm_irreversible="drop shipments.label")):
            try:
                report = evolution.run_evolution(str(contract), options)
            except evolution.EvolutionError as exc:
                report = {"state": "blocked", "applied": {"state": "not_applied"}, "error": str(exc)}
            self.assertEqual(report["state"], "blocked", report.get("plan", {}).get("blockers"))
            self.assertEqual(report["applied"]["state"], "not_applied")
            self.assertEqual(tree(project), before)
        self.assert_clean(project)

    def test_the_apply_report_separates_files_from_the_database_and_the_deployment(self) -> None:
        project = self.copy(ORDERS, "orders")
        doc = json.loads((EXAMPLES / "evolutions" / "orders-add-shipments.json").read_text())
        doc["project"]["path"] = str(project)
        report = evolution.run_evolution(str(self.write_json("ship.json", doc)), evolution.EvolveOptions(apply=True, verify=False))
        applied = report["applied"]
        self.assertEqual(applied["state"], "applied", report["plan"]["blockers"])
        self.assertEqual(applied["scope"], "files of the project only")
        self.assertEqual((applied["database"]["migration_required"], applied["database"]["applied_by_this_tool"]), (True, False))
        self.assertIn("alembic upgrade head", applied["database"]["status"])
        self.assertIn("not performed by this tool", applied["deployment"])
        self.assert_clean(project)


class CommandLineTests(Case):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=str(REPO_ROOT), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)

    def test_recover_refuses_a_strategy_switch_and_unlock_needs_the_other_host_named(self) -> None:
        project = self.small_project()
        files, expected = self.plan(project)
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="x", faults=journal.FaultInjector(points=["after_publish_all"]))
        op = journal.pending_operations(project)[0]
        with self.assertRaises(journal.InjectedFailure):
            journal.recover(project, op, strategy="rollback", faults=journal.FaultInjector(points=["recover_after:1"]))
        proc = self.run_cli("recover", str(project), "--forward")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("resume with the same strategy", proc.stderr)
        proc = self.run_cli("recover", str(project))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rolled_back", proc.stdout)
        self.assertIn("directories removed", proc.stdout)
        self.assert_old(project)
        path = project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": 1, "host": "elsewhere", "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "far away", "token": "z"}), encoding="utf-8")
        proc = self.run_cli("ops", "list", str(project))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lock: unknown", proc.stdout)
        proc = self.run_cli("ops", "unlock", str(project))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--other-host elsewhere", proc.stdout)
        self.assertTrue(path.exists())
        proc = self.run_cli("ops", "unlock", str(project), "--other-host", "nope")
        self.assertEqual(proc.returncode, 1)
        proc = self.run_cli("ops", "unlock", str(project), "--other-host", "elsewhere")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("confirmed by the operator", proc.stdout)
        self.assertIsNone(locking.inspect(project))


if __name__ == "__main__":
    unittest.main()
