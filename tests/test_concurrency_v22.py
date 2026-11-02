"""Increment 5 of v2.2: concurrency with real processes. Two evolutions of one project, an evolution against an
adoption, a merge against an evolution, a recovery against an apply, two recoveries at once, a lock held by another
process, a bounded wait that times out, a process killed without releasing, a corrupt lock and a lock of another
host. Every case ends with the invariant checked: the tree is one consistent outcome (never a mix of two writers),
no operation is pending, no temporary is left, the lock is free or belongs to whom it says.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict, List

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_common.artefacts import walk_files  # noqa: E402
from fastapi_design import adoption, evolution, journal, locking  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
TEAM_NOTES = EXAMPLES / "generated" / "team-notes"
WORKSHOP = FIXTURES / "adoption22" / "workshop"
CLI = SKILL_SCRIPTS / "evolve_fastapi.py"
SCHEMA = evolution.EVOLUTION_SCHEMA
WORKSHOP_ADOPTION = {"modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}], "package": "app", "unmanaged": ["app/routers/users.py", "app/storage.py"], "mount": {"file": "app/main.py", "function": "create_app", "app": "application", "after_call": "application.include_router", "prefix": "/api"}, "new_modules": {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": "app.main", "attribute": "app"}}, "dependencies": {"allowed": []}, "operations": ["add_module", "mount_router", "add_endpoint", "add_field"]}

DRIVER = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from fastapi_design import adoption, evolution, journal, locking
action, project, args = sys.argv[2], Path(sys.argv[3]), json.loads(sys.argv[4])


def wait_for(path, timeout=60.0):
    deadline = time.time() + timeout
    while not Path(path).exists():
        if time.time() > deadline:
            raise SystemExit("barrier timeout")
        time.sleep(0.002)


out = {"pid": os.getpid()}
try:
    if action == "hold":
        lock = locking.acquire(project, kind=args.get("kind", "evolve"), description=args.get("description", "holder"))
        Path(args["ready"]).write_text("ready")
        if args.get("seconds") is not None:
            time.sleep(args["seconds"])
        else:
            wait_for(args["release"], timeout=120)
        out["released"] = lock.release()
    elif action == "evolve":
        if args.get("barrier"):
            wait_for(args["barrier"])
        report = evolution.run_evolution(args["contract"], evolution.EvolveOptions(apply=True, verify=False, allow_merge=bool(args.get("allow_merge"))))
        out.update({"state": report["state"], "applied": report["applied"]["state"], "blockers": sorted({b.split(":")[0] for b in report["plan"]["blockers"]}), "error": report["applied"].get("error"), "holder_pid": ((report["applied"].get("lock") or {}).get("holder") or {}).get("pid")})
    elif action == "recover":
        if args.get("barrier"):
            wait_for(args["barrier"])
        reports = [journal.recover(project, op, strategy=args.get("strategy", "rollback"), wait_s=float(args.get("wait_s", 0.0))).to_dict() for op in journal.pending_operations(project)]
        out["recovered"] = [r["state"] for r in reports]
    elif action == "adopt":
        if args.get("barrier"):
            wait_for(args["barrier"])
        contract, errors = adoption.validate_adoption(json.loads(Path(args["contract"]).read_text()))
        out["written"] = adoption.adopt(contract)["written"]
except locking.LockHeld as exc:
    out.update({"locked": exc.reason, "holder_pid": exc.holder.get("pid")})
except adoption.AdoptionError as exc:
    out["adoption_error"] = exc.errors
except Exception as exc:  # noqa: BLE001
    out.update({"error": type(exc).__name__, "message": str(exc)[:300]})
print(json.dumps(out))
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


class ConcurrencyCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-concurrency22-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.children: List[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv", ".fastapi-architect/ops"))
        return target

    def write_json(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def spawn(self, action: str, project: Path, **args) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", DRIVER, str(SKILL_SCRIPTS), action, str(project), json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.children.append(child)
        return child

    def result(self, child: subprocess.Popen, timeout: float = 120.0) -> dict:
        out, err = child.communicate(timeout=timeout)
        self.assertTrue(out.strip(), err[-2000:])
        return json.loads(out.strip().splitlines()[-1])

    def holder(self, project: Path, name: str = "holder", **args) -> subprocess.Popen:
        ready = self.tmp / f"{name}.ready"
        release = self.tmp / f"{name}.release"
        child = self.spawn("hold", project, ready=str(ready), release=str(release), **args)
        deadline = time.time() + 30
        while not ready.exists():
            if child.poll() is not None:  # only then is stderr safe to read: reading it earlier blocks until the child exits
                self.fail("the holder exited before taking the lock: " + (child.stderr.read()[-2000:] if child.stderr else ""))
            self.assertLess(time.time(), deadline, "the holder did not take the lock")
            time.sleep(0.005)
        return child

    def release(self, name: str = "holder") -> None:
        (self.tmp / f"{name}.release").write_text("go")

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=str(REPO_ROOT), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=300)

    def assert_settled(self, project: Path, lock_free: bool = True) -> None:
        self.assertEqual(journal.pending_operations(project), [])
        self.assertEqual(journal.unreadable_operations(project), [])
        self.assertEqual(journal.residual_temporaries(project), [])
        if lock_free:
            self.assertIsNone(locking.inspect(project))

    def inventory_contract(self, project: Path, name: str = "suppliers.json") -> Path:
        doc = json.loads((EXAMPLES / "evolutions" / "inventory-add-suppliers.json").read_text())
        doc["project"]["path"] = str(project)
        return self.write_json(name, doc)

    def pending_inventory(self) -> tuple:
        project = self.copy(INVENTORY, "inv")
        contract = self.inventory_contract(project)
        before = tree(project)
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, faults=journal.FaultInjector(points=["after_publish:2"])))
        self.assertEqual(report["applied"]["state"], "interrupted")
        return project, contract, before


class TwoWritersTests(ConcurrencyCase):
    def test_two_evolutions_of_the_same_project_apply_exactly_once_and_leave_one_tree(self) -> None:
        reference = self.copy(INVENTORY, "ref")
        ref_report = evolution.run_evolution(str(self.inventory_contract(reference, "ref.json")), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(ref_report["applied"]["state"], "applied")
        expected = tree(reference)
        manifest_files = json.loads((reference / ".fastapi-architect" / "generation.json").read_text())["files"]
        seen = set()
        for round_ in range(3):
            project = self.copy(INVENTORY, f"inv{round_}")
            contract = self.inventory_contract(project, f"c{round_}.json")
            barrier = self.tmp / f"go{round_}"
            a = self.spawn("evolve", project, contract=str(contract), barrier=str(barrier))
            b = self.spawn("evolve", project, contract=str(contract), barrier=str(barrier))
            time.sleep(0.4)  # both interpreters are up and waiting at the barrier
            barrier.write_text("go")
            results = [self.result(a), self.result(b)]
            applied = [r for r in results if r.get("applied") == "applied"]
            self.assertEqual(len(applied), 1, results)
            other = [r for r in results if r is not applied[0]][0]
            outcome = other.get("locked") or other.get("applied")
            self.assertIn(outcome, ("held", "locked", "nothing_to_apply", "not_applied", "write_failed"), other)
            if other.get("applied") == "not_applied":
                self.assertTrue(other["blockers"], other)
            seen.add(outcome)
            actual = tree(project)
            manifest = ".fastapi-architect/generation.json"
            self.assertEqual({k: v for k, v in actual.items() if k != manifest}, {k: v for k, v in expected.items() if k != manifest}, "the tree is the one a single apply produces")
            self.assertEqual(json.loads((project / manifest).read_text())["files"], manifest_files, "the manifest records the same files (its history names the operation)")
            self.assert_settled(project)
        self.assertTrue(seen, seen)

    def test_an_adoption_refuses_at_once_while_an_evolution_holds_the_project(self) -> None:
        shop = self.copy(WORKSHOP, "shop")
        contract_path = self.write_json("adopt.json", {"schema": adoption.ADOPTION_SCHEMA_11, "project": {"path": str(shop)}, **WORKSHOP_ADOPTION})
        before = tree(shop)
        holder = self.holder(shop, kind="evolve", description="a long evolution")
        try:
            started = time.time()
            contract, errors = adoption.validate_adoption(json.loads(contract_path.read_text()))
            self.assertEqual(errors, [])
            with self.assertRaises(locking.LockHeld) as ctx:
                adoption.adopt(contract)
            self.assertLess(time.time() - started, 5.0, "fail fast")
            self.assertEqual((ctx.exception.reason, ctx.exception.holder.get("pid")), ("held", holder.pid))
            proc = self.cli("adopt", str(contract_path))
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("locked by pid", proc.stderr)
            self.assertEqual(tree(shop), before)
        finally:
            self.release()
        self.assertEqual(self.result(holder)["released"], True)
        self.assertEqual(adoption.adopt(contract)["written"], [adoption.ADOPTION])
        self.assert_settled(shop)

    def test_a_merge_evolution_and_a_plain_evolution_never_interleave(self) -> None:
        project = self.copy(TEAM_NOTES, "notes")
        merge = self.write_json("merge.json", {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "reminders", "entities": [{"name": "Reminder", "fields": [{"name": "text", "type": "str", "max_length": 80}], "operations": ["create", "get", "list"]}]}], "files": {"merge": [{"path": "src/team_notes/main.py", "transform": "three_way_python"}]}})
        plain = self.write_json("plain.json", {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "attachments", "entities": [{"name": "Attachment", "fields": [{"name": "filename", "type": "str", "max_length": 120}], "operations": ["create", "get", "list"]}]}]})
        barrier = self.tmp / "go"
        a = self.spawn("evolve", project, contract=str(merge), barrier=str(barrier), allow_merge=True)
        b = self.spawn("evolve", project, contract=str(plain), barrier=str(barrier))
        time.sleep(0.4)
        barrier.write_text("go")
        results = [self.result(a), self.result(b)]
        applied = [r for r in results if r.get("applied") == "applied"]
        self.assertGreaterEqual(len(applied), 1, results)
        self.assert_settled(project)
        # whatever lost runs again, sequentially, on the tree the winner left
        for contract, allow in ((merge, True), (plain, False)):
            report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, allow_merge=allow))
            self.assertIn(report["applied"]["state"], ("applied", "nothing_to_apply"), report["plan"]["blockers"])
        main = (project / "src" / "team_notes" / "main.py").read_text()
        self.assertIn("reminders", main)
        self.assertIn("attachments", main)
        recorded = json.loads((project / ".fastapi-architect" / "generation.json").read_text())["files"]
        self.assertTrue(any("attachments" in rel for rel in recorded), sorted(recorded)[:20])
        self.assertTrue(any("reminders" in rel for rel in recorded))
        self.assert_settled(project)


class RecoveryConcurrencyTests(ConcurrencyCase):
    def test_recover_and_apply_exclude_each_other(self) -> None:
        project, contract, before = self.pending_inventory()
        holder = self.holder(project, kind="evolve", description="someone else")
        try:
            proc = self.cli("recover", str(project))
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn(f"locked by pid {holder.pid}", proc.stderr)
            self.assertEqual(len(journal.pending_operations(project)), 1, "nothing was recovered under someone else's lock")
            report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
            self.assertEqual([b.split(":")[0] for b in report["plan"]["blockers"]], ["no_pending_operation"])
        finally:
            self.release()
        self.result(holder)
        proc = self.cli("recover", str(project))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(tree(project), before)
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assert_settled(project)

    def test_two_simultaneous_recoveries_recover_exactly_once(self) -> None:
        project, _contract, before = self.pending_inventory()
        barrier = self.tmp / "go"
        a = self.spawn("recover", project, barrier=str(barrier))
        b = self.spawn("recover", project, barrier=str(barrier))
        time.sleep(0.4)
        barrier.write_text("go")
        results = [self.result(a), self.result(b)]
        rolled = [r for r in results if r.get("recovered") == ["rolled_back"]]
        self.assertEqual(len(rolled), 1, results)
        other = [r for r in results if r is not rolled[0]][0]
        self.assertTrue(other.get("locked") == "held" or other.get("recovered") == [], other)
        self.assertEqual(tree(project), before)
        self.assert_settled(project)
        # a third recovery finds nothing: idempotent
        self.assertEqual(self.result(self.spawn("recover", project))["recovered"], [])


class LockTests(ConcurrencyCase):
    def test_a_lock_held_by_another_process_is_named_and_an_apply_fails_fast(self) -> None:
        project = self.copy(INVENTORY, "inv")
        contract = self.inventory_contract(project)
        holder = self.holder(project, kind="evolve", description="long running")
        try:
            state = locking.inspect(project)
            self.assertEqual((state["status"], state["pid"], state["same_host"], state["holder_alive"]), ("held", holder.pid, True, True))
            self.assertNotEqual(state["pid"], os.getpid())
            proc = self.cli("ops", "list", str(project))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"lock: held by pid {holder.pid}", proc.stdout)
            started = time.time()
            report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
            self.assertLess(time.time() - started, 10.0)
            self.assertEqual(report["applied"]["state"], "locked")
            self.assertEqual(report["applied"]["lock"]["holder"]["pid"], holder.pid)
            self.assertEqual(report["applied"]["lock"]["reason"], "held")
            plan = evolution.plan_evolution(evolution.load_evolution(str(contract)))
            self.assertEqual(plan.state, "planned", "planning is read-only and does not need the lock")
        finally:
            self.release()
        self.result(holder)
        self.assert_settled(project)

    def test_a_bounded_wait_times_out_and_a_longer_one_outlives_the_holder(self) -> None:
        project, _contract, before = self.pending_inventory()
        holder = self.holder(project, kind="evolve", description="busy for a while", seconds=2.5)
        op = journal.pending_operations(project)[0]
        started = time.time()
        with self.assertRaises(locking.LockHeld) as ctx:
            journal.recover(project, op, strategy="rollback", wait_s=0.5)
        waited = time.time() - started
        self.assertGreaterEqual(waited, 0.45)
        self.assertLess(waited, 2.0, "the wait is bounded")
        self.assertEqual((ctx.exception.reason, ctx.exception.holder.get("pid")), ("held", holder.pid))
        started = time.time()
        report = journal.recover(project, op, strategy="rollback", wait_s=20.0)
        self.assertEqual(report.state, "rolled_back")
        self.assertLess(time.time() - started, 15.0)
        self.result(holder)
        self.assertEqual(tree(project), before)
        self.assert_settled(project)

    def test_a_process_killed_without_releasing_leaves_a_stale_lock_removed_only_on_request(self) -> None:
        project = self.copy(INVENTORY, "inv")
        contract = self.inventory_contract(project)
        holder = self.holder(project, kind="evolve", description="about to die", seconds=60)
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        state = locking.inspect(project)
        self.assertEqual((state["status"], state["pid"], state["holder_alive"]), ("stale", holder.pid, False))
        with self.assertRaises(locking.LockHeld) as ctx:
            locking.acquire(project, kind="evolve")
        self.assertEqual(ctx.exception.reason, "stale")
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual((report["applied"]["state"], report["applied"]["lock"]["reason"]), ("locked", "stale"))
        proc = self.cli("ops", "list", str(project))
        self.assertEqual(proc.returncode, 1, "a stale lock asks for attention")
        self.assertIn("lock: stale", proc.stdout)
        proc = self.cli("ops", "unlock", str(project))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lock removed (stale)", proc.stdout)
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assert_settled(project)

    def test_a_corrupt_lock_is_unreadable_until_an_operator_removes_it(self) -> None:
        project = self.copy(INVENTORY, "inv")
        contract = self.inventory_contract(project)
        path = project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": "fastapi-architect/lock/1.0", "pid": 4', encoding="utf-8")
        self.assertEqual(locking.inspect(project)["status"], "unreadable")
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual((report["applied"]["state"], report["applied"]["lock"]["reason"]), ("locked", "unreadable"))
        self.assertTrue(path.exists(), "never broken silently")
        proc = self.cli("ops", "unlock", str(project))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lock removed (unreadable)", proc.stdout)
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assert_settled(project)

    def test_a_lock_of_another_host_is_never_broken_by_age(self) -> None:
        project = self.copy(INVENTORY, "inv")
        contract = self.inventory_contract(project)
        path = project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": 4242, "host": "build-agent-7", "started_at": "2020-01-01T00:00:00+00:00", "kind": "evolve", "description": "a CI job", "token": "t"}), encoding="utf-8")
        state = locking.inspect(project)
        self.assertEqual((state["status"], state["older_than_stale_after"]), ("unknown", True))
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual((report["applied"]["state"], report["applied"]["lock"]["reason"]), ("locked", "unknown"))
        with self.assertRaises(locking.LockHeld):
            locking.acquire(project, kind="evolve", break_stale=True)
        for args in ((), ("--other-host", "build-agent-8")):
            proc = self.cli("ops", "unlock", str(project), *args)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertTrue(path.exists())
        proc = self.cli("ops", "unlock", str(project), "--other-host", "build-agent-7")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNone(locking.inspect(project))
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assert_settled(project)


if __name__ == "__main__":
    unittest.main()
