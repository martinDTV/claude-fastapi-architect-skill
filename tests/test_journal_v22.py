"""v2.2 increment 0: the project lock (fail-fast, bounded wait, stale handling), journal 1.1 (steps, parent_op,
1.0 still read), concurrency between writers, interruptions with steps, and the ``ops`` subcommands.

Deterministic: every wait is bounded, every "other process" is a thread or a child process this module controls, and
every stale lock is a record this module writes with a pid that is known to be gone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_design import journal, locking  # noqa: E402

EVOLVE = SKILL_SCRIPTS / "evolve_fastapi.py"


def _dead_pid() -> int:
    """A pid that belonged to a process that has exited (the child ran and was reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class LockCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-lock-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name).resolve() / "project"
        self.project.mkdir()
        (self.project / "a.txt").write_text("a0\n", encoding="utf-8")
        (self.project / "b.txt").write_text("b0\n", encoding="utf-8")

    def plan(self, suffix: str = "1") -> tuple:
        files = {"a.txt": f"a{suffix}\n", "b.txt": f"b{suffix}\n"}
        expected = {rel: journal.sha256_bytes((self.project / rel).read_bytes()) for rel in files}
        return files, expected

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(EVOLVE), *args], capture_output=True, text=True, cwd=str(REPO_ROOT))


class LockTests(LockCase):
    def test_acquire_release_and_inspect(self) -> None:
        self.assertIsNone(locking.inspect(self.project))
        lock = locking.acquire(self.project, kind="evolve", description="test")
        held = locking.inspect(self.project)
        self.assertEqual((held["status"], held["pid"], held["kind"], held["holder_alive"]), ("held", os.getpid(), "evolve", True))
        self.assertEqual(json.loads(lock.path.read_text(encoding="utf-8"))["schema"], locking.LOCK_SCHEMA)
        self.assertTrue(lock.release())
        self.assertIsNone(locking.inspect(self.project))
        self.assertFalse((self.project / journal.OPS_DIR).exists(), "an empty ops directory is removed with the lock")
        self.assertFalse(lock.release(), "a second release does nothing")

    def test_fail_fast_names_the_holder(self) -> None:
        with locking.project_lock(self.project, kind="evolve", description="first"):
            with self.assertRaises(locking.LockHeld) as held:
                locking.acquire(self.project, kind="evolve", description="second")
            self.assertEqual((held.exception.reason, held.exception.holder["description"]), ("held", "first"))
            self.assertIn("locked by pid", str(held.exception))
        self.assertIsNone(locking.inspect(self.project))

    def test_bounded_wait_succeeds_when_the_holder_leaves_and_times_out_otherwise(self) -> None:
        first = locking.acquire(self.project, kind="evolve", description="slow")
        timer = threading.Timer(0.3, first.release)
        timer.start()
        started = time.monotonic()
        second = locking.acquire(self.project, kind="evolve", description="patient", wait_s=5.0, poll_s=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual(second.record["description"], "patient")
        with self.assertRaises(locking.LockHeld):
            locking.acquire(self.project, kind="evolve", description="impatient", wait_s=0.2, poll_s=0.05)
        second.release()
        timer.cancel()

    def test_a_stale_lock_is_reported_never_broken_silently_and_removable_on_request(self) -> None:
        path = self.project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True)
        record = {"schema": locking.LOCK_SCHEMA, "pid": _dead_pid(), "host": __import__("socket").gethostname(), "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "crashed", "token": "x"}
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(locking.inspect(self.project)["status"], "stale")
        with self.assertRaises(locking.LockHeld) as held:
            locking.acquire(self.project, kind="evolve", description="next")
        self.assertEqual(held.exception.reason, "stale")
        taken = locking.acquire(self.project, kind="evolve", description="next", break_stale=True)
        self.assertEqual(taken.record["broke"]["description"], "crashed")
        taken.release()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        removed = locking.unlock_stale(self.project)
        self.assertEqual((removed["removed"], removed["reason"]), (True, "stale"))
        with locking.project_lock(self.project, kind="evolve", description="alive"):
            refused = locking.unlock_stale(self.project)
            self.assertEqual((refused["removed"], refused["reason"]), (False, "held"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(locking.inspect(self.project)["status"], "unreadable")
        self.assertTrue(locking.unlock_stale(self.project)["removed"])

    def test_a_lock_from_another_host_is_stale_only_after_the_declared_age(self) -> None:
        path = self.project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True)
        record = {"schema": locking.LOCK_SCHEMA, "pid": 1, "host": "another-host", "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "elsewhere", "token": "y"}
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(locking.inspect(self.project, stale_after_s=10**9)["status"], "held")
        self.assertEqual(locking.inspect(self.project)["status"], "stale")  # far older than the default six hours


class WriterUnderLockTests(LockCase):
    def test_a_write_refuses_at_once_while_another_operation_holds_the_project(self) -> None:
        files, expected = self.plan()
        with locking.project_lock(self.project, kind="evolve", description="holder"):
            with self.assertRaises(locking.LockHeld):
                journal.write_recoverable(self.project, files, expected, kind="evolve", description="second")
            self.assertEqual(sorted(p.name for p in (self.project / journal.OPS_DIR).iterdir()), [locking.LOCK_FILE], "nothing was staged")
        self.assertEqual((self.project / "a.txt").read_text(encoding="utf-8"), "a0\n")
        op = journal.write_recoverable(self.project, files, expected, kind="evolve", description="after")
        self.assertEqual(op.state, "published")
        self.assertIsNone(locking.inspect(self.project))

    def test_two_writers_racing_from_two_processes_publish_exactly_once_each_in_turn(self) -> None:
        files, expected = self.plan()
        script = (
            "import sys, json; sys.path.insert(0, sys.argv[1]); from fastapi_design import journal, locking\n"
            "from pathlib import Path; p = Path(sys.argv[2]); files = json.loads(sys.argv[3]); expected = json.loads(sys.argv[4])\n"
            "try:\n    op = journal.write_recoverable(p, files, expected, kind='evolve', description='child', wait_s=float(sys.argv[5]))\n    print('published', op.state)\nexcept locking.LockHeld as exc:\n    print('locked', exc.reason)\nexcept Exception as exc:\n    print('error', type(exc).__name__)\n"
        )
        argv = [sys.executable, "-c", script, str(SKILL_SCRIPTS), str(self.project), json.dumps(files), json.dumps(expected)]
        with locking.project_lock(self.project, kind="evolve", description="parent"):
            fast = subprocess.run(argv + ["0"], capture_output=True, text=True)
            self.assertEqual(fast.stdout.strip(), "locked held", fast.stderr[-1000:])
            patient = subprocess.Popen(argv + ["5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.4)
        out, err = patient.communicate(timeout=30)
        self.assertEqual(out.strip(), "published published", err[-1000:])
        self.assertEqual((self.project / "a.txt").read_text(encoding="utf-8"), "a1\n")
        late = subprocess.run(argv + ["0"], capture_output=True, text=True)
        self.assertEqual(late.stdout.strip(), "error WriteError", "the same plan again meets changed digests: refused by content, not by the lock")

    def test_two_threads_with_a_bounded_wait_serialise_on_disjoint_files(self) -> None:
        (self.project / "c.txt").write_text("c0\n", encoding="utf-8")
        results: dict = {}

        def writer(name: str, rel: str) -> None:
            files = {rel: f"{name}\n"}
            expected = {rel: journal.sha256_bytes((self.project / rel).read_bytes())}
            try:
                op = journal.write_recoverable(self.project, files, expected, kind="evolve", description=name, wait_s=5.0)
                results[name] = op.state
            except locking.LockHeld as exc:
                results[name] = f"locked:{exc.reason}"

        threads = [threading.Thread(target=writer, args=("one", "a.txt")), threading.Thread(target=writer, args=("two", "c.txt"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(results, {"one": "published", "two": "published"})
        self.assertEqual(((self.project / "a.txt").read_text(encoding="utf-8"), (self.project / "c.txt").read_text(encoding="utf-8")), ("one\n", "two\n"))
        self.assertIsNone(locking.inspect(self.project))


class JournalStepsTests(LockCase):
    def test_steps_and_parent_survive_an_interruption_and_recovery(self) -> None:
        files, expected = self.plan()
        steps = [{"kind": "merge", "paths": ["a.txt"], "evidence": {"base_sha256": "x", "hunks": {"ours": 1, "theirs": 1}}}, {"kind": "manifest", "paths": ["b.txt"]}]
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(self.project, files, expected, kind="evolve", description="phased", faults=journal.FaultInjector({"after_publish:1"}), steps=steps, parent_op="0123456789abcdef")
        self.assertIsNone(locking.inspect(self.project), "the lock is released even when the publication is interrupted")
        pending = journal.pending_operations(self.project)
        self.assertEqual(len(pending), 1)
        op = pending[0]
        self.assertEqual((op.state, op.steps, op.parent_op), ("publishing", steps, "0123456789abcdef"))
        on_disk = json.loads((self.project / op.directory / journal.JOURNAL).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], "fastapi-architect/operation/1.1")
        self.assertEqual(journal.classify(self.project, op), {"a.txt": "published", "b.txt": "pending"})
        report = journal.recover(self.project, op, strategy="forward")
        self.assertEqual(report.state, "completed")
        self.assertEqual((self.project / "b.txt").read_text(encoding="utf-8"), "b1\n")
        self.assertIsNone(locking.inspect(self.project))

    def test_a_journal_10_without_steps_is_read_as_before(self) -> None:
        op_dir = self.project / journal.OPS_DIR / "00000000deadbeef"
        (op_dir / "new").mkdir(parents=True)
        (op_dir / "new" / "a.txt").write_text("a1\n", encoding="utf-8")
        (op_dir / "backup").mkdir()
        (op_dir / "backup" / "a.txt").write_text("a0\n", encoding="utf-8")
        legacy = {"schema": "fastapi-architect/operation/1.0", "op_id": "00000000deadbeef", "kind": "evolve", "description": "legacy", "state": "publishing", "files": [{"path": "a.txt", "expected": journal.sha256_bytes(b"a0\n"), "new": journal.sha256_bytes(b"a1\n")}], "published": [], "recovered": [], "conflicts": {}}
        (op_dir / journal.JOURNAL).write_text(json.dumps(legacy), encoding="utf-8")
        ops = journal.pending_operations(self.project)
        self.assertEqual((len(ops), ops[0].steps, ops[0].parent_op), (1, [], None))
        self.assertEqual(journal.unreadable_operations(self.project), [])
        self.assertEqual(journal.recover(self.project, ops[0], strategy="rollback").state, "rolled_back")

    def test_the_operation_identity_ignores_steps(self) -> None:
        files, expected = self.plan()
        plain = journal.operation_id(files, expected, "evolve", "same")
        op = journal.prepare(self.project, files, expected, kind="evolve", description="same", steps=[{"kind": "regenerate", "paths": ["a.txt"]}])
        self.assertEqual(op.op_id, plain, "steps describe the work; the identity is the plan")
        journal.complete(self.project, op)


class OpsCommandTests(LockCase):
    def test_list_show_and_unlock(self) -> None:
        free = self.cli("ops", "list", str(self.project))
        self.assertEqual((free.returncode, free.stdout), (0, "lock: free\npending operations: none\n"))
        files, expected = self.plan()
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(self.project, files, expected, kind="evolve", description="phased", faults=journal.FaultInjector({"after_publish:1"}), steps=[{"kind": "regenerate", "paths": ["a.txt", "b.txt"]}])
        listed = self.cli("ops", "list", str(self.project), "--format", "json")
        payload = json.loads(listed.stdout)
        self.assertEqual((listed.returncode, payload["lock"], payload["pending"][0]["state"], payload["pending"][0]["steps"]), (0, None, "publishing", ["regenerate"]))
        shown = self.cli("ops", "show", str(self.project), "--op", payload["pending"][0]["op_id"])
        self.assertEqual(shown.returncode, 0)
        self.assertIn("files a.txt=published, b.txt=pending", shown.stdout)
        self.assertIn('"kind": "regenerate"', shown.stdout)
        with locking.project_lock(self.project, kind="evolve", description="busy"):
            held = self.cli("ops", "list", str(self.project))
            self.assertEqual(held.returncode, 0)
            self.assertIn("lock: held by pid", held.stdout)
            refused = self.cli("ops", "unlock", str(self.project))
            self.assertEqual(refused.returncode, 1)
            self.assertIn("lock not removed: held", refused.stdout)
            recovering = self.cli("recover", str(self.project), "--forward")
            self.assertEqual(recovering.returncode, 3, recovering.stderr)
            self.assertIn("locked", recovering.stderr)
        path = self.project / journal.OPS_DIR / locking.LOCK_FILE
        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": _dead_pid(), "host": __import__("socket").gethostname(), "started_at": "2026-01-01T00:00:00+00:00", "kind": "evolve", "description": "crashed", "token": "z"}), encoding="utf-8")
        stale = self.cli("ops", "list", str(self.project))
        self.assertEqual(stale.returncode, 1, "a stale lock needs attention")
        unlocked = self.cli("ops", "unlock", str(self.project))
        self.assertEqual((unlocked.returncode, unlocked.stdout), (0, "lock removed (stale)\n"))
        recovered = self.cli("recover", str(self.project), "--forward")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual((self.project / "b.txt").read_text(encoding="utf-8"), "b1\n")
        shutil.rmtree(self.project / ".fastapi-architect", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
