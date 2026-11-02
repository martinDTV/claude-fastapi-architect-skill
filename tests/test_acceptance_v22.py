"""Acceptance of v2.2.0 (increment 6): one reproducible harness that walks the interfaces and the real transitions of
the release on the repository's own fixtures and specifications, and writes the evidence it produced.

In-memory flow (always): design and generation of `team-notes` from its specification, the static audit and the
SARIF document, an evolution that adds a module (verified with the generated suite through the trust model when
FastAPI is importable, audited otherwise), a hand edit on the integration point preserved by the opt-in three-way merge
(and refused without the opt-in), the adoption of the hand-written `workshop` with a generated module mounted and a
bounded extension, an interrupted publication rolled back and retried, the interfaces of the phased rename and of the
controlled contract on `parcels` (planned, never applied without a database), and the recovery properties increment 5
promised (`completed` never publishes twice, a recovery interrupted twice still finishes, a reused pid is a stale
lock). The evidence is a JSON document under ``FA_ACCEPTANCE_EVIDENCE`` when set (a temporary directory otherwise).

PostgreSQL flow (opt-in, ``FA_DESIGN_PG=1``): the rename cycle A → expand → B → retire → C → contract → D on a
throwaway cluster, composed from the helpers of the retire module; the finer controls of each stage (N-1 traffic,
contradictory values, the catalogue dependencies, the sessions, the lock timeout, the downgrade) live in
``test_renames_v22_postgresql.py``, ``test_retire_contract_v22_postgresql.py`` and
``test_destructive_acceptance_v22_postgresql.py`` and are run with it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.engine import audit_project  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_audit.report import to_sarif  # noqa: E402
from fastapi_common.artefacts import walk_files  # noqa: E402
from fastapi_common.version import TOOL_VERSION  # noqa: E402
from fastapi_design import adoption, evolution, journal, locking, pipeline  # noqa: E402
from fastapi_design.generator import read_manifest, verify_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
SPECS = EXAMPLES / "specs"
PARCELS = EXAMPLES / "generated" / "parcels"
WORKSHOP = FIXTURES / "adoption22" / "workshop"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx"))
SCHEMA = evolution.EVOLUTION_SCHEMA
MAIN = "src/team_notes/main.py"
INVARIANTS = ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"]
WORKSHOP_ADOPTION = {"modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}], "package": "app", "unmanaged": ["app/routers/users.py", "app/storage.py"], "mount": {"file": "app/main.py", "function": "create_app", "app": "application", "after_call": "application.include_router", "prefix": "/api"}, "new_modules": {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": "app.main", "attribute": "app"}}, "dependencies": {"allowed": ["app.deps:require_api_key", "app.services:count_items", "app.services:catalogue_value"]}, "operations": ["add_module", "mount_router", "add_endpoint", "add_field"]}
EVIDENCE_DIR = os.environ.get("FA_ACCEPTANCE_EVIDENCE")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


class AcceptanceFlowTests(unittest.TestCase):
    """The in-memory flow: every step records what it did under ``self.evidence``; the last test writes it out."""

    evidence: Dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="fa-acceptance22-")
        cls.tmp = Path(cls._tmp.name).resolve()
        cls.evidence = {"schema": "fastapi-architect/acceptance-evidence/2.2", "tool_version": TOOL_VERSION, "python": sys.version.split()[0], "fastapi_importable": HAS_FASTAPI, "steps": {}}

    @classmethod
    def tearDownClass(cls) -> None:
        out = Path(EVIDENCE_DIR) if EVIDENCE_DIR else cls.tmp
        out.mkdir(parents=True, exist_ok=True)
        (out / "acceptance-v2.2-evidence.json").write_text(json.dumps(cls.evidence, indent=2, sort_keys=True, default=str), encoding="utf-8")
        cls._tmp.cleanup()

    def record(self, step: str, **facts: Any) -> None:
        self.evidence["steps"][step] = facts

    def write_json(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def trusted(self, project: Path, **extra: Any) -> evolution.EvolveOptions:
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        if not HAS_FASTAPI:
            return evolution.EvolveOptions(apply=True, verify=False, **extra)
        return evolution.EvolveOptions(apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: acceptance of v2.2.0"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=600), test_command=command, **extra)

    def settled(self, project: Path) -> None:
        self.assertEqual(journal.pending_operations(project), [])
        self.assertEqual(journal.unreadable_operations(project), [])
        self.assertEqual(journal.residual_temporaries(project), [])
        self.assertIsNone(locking.inspect(project))

    # 1. design and generation ------------------------------------------------- #

    def test_01_design_and_generation_from_the_specification(self) -> None:
        target = self.tmp / "team-notes"
        started = time.time()
        report = pipeline.run_design(str(SPECS / "team-notes.json"), pipeline.DesignOptions(target=str(target), audit=True))
        elapsed = round(time.time() - started, 2)
        self.assertEqual(report["generation"]["conflicts"], [])
        manifest = read_manifest(str(target))
        self.assertEqual(manifest["tool"]["version"], TOOL_VERSION)
        status = verify_manifest(str(target))["status"]
        self.assertTrue(all(v == "identical" for v in status.values()), {k: v for k, v in status.items() if v != "identical"})
        self.assertIsNotNone(report["verification"]["audit"])
        self.settled(target)
        self.__class__.project = target
        self.record("01_design_generation", spec="examples/specs/team-notes.json", files=len(manifest["files"]), manifest_schema=manifest["schema"], tool_version=manifest["tool"]["version"], recommendation=report.get("recommendation", {}).get("style") if isinstance(report.get("recommendation"), dict) else None, seconds=elapsed)

    # 2. audit and verification ------------------------------------------------- #

    def test_02_audit_and_sarif_of_the_generated_project(self) -> None:
        project = self.__class__.project
        doc = audit_project(str(project))
        sarif = json.loads(to_sarif(doc))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(len(sarif["runs"]), 1)
        summary = doc.get("summary", {})
        blocking = [f for f in doc.get("findings", []) if f.get("severity") in ("critical", "high")]
        self.assertEqual(blocking, [], "a freshly generated project has no high or critical finding")
        verified = None
        if HAS_FASTAPI:
            from fastapi_design.verify import run_generated_tests

            command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
            result = run_generated_tests(str(project), Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: acceptance of v2.2.0"), ExecutionPolicy(authorized=True, command=command, timeout_s=600), command, "local")
            verified = (result.get("run") or {}).get("outcome") if isinstance(result, dict) else None
            self.assertEqual(verified, "passed", json.dumps(result, default=str)[-1500:])
        self.record("02_audit_verification", findings=len(doc.get("findings", [])), by_severity=summary.get("by_severity"), sarif_version=sarif["version"], generated_suite=verified or "not executed (FastAPI not importable here)")

    # 3. evolution --------------------------------------------------------------- #

    def test_03_evolution_adds_a_module_and_repeats_as_a_noop(self) -> None:
        project = self.__class__.project
        contract = self.write_json("add-reminders.json", {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "reminders", "description": "reminders", "entities": [{"name": "Reminder", "fields": [{"name": "text", "type": "str", "max_length": 80}], "operations": ["create", "get", "list"]}]}], "invariants": INVARIANTS})
        before = tree(project)
        report = evolution.run_evolution(str(contract), self.trusted(project))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"] or report.get("verification", {}).get("reasons"))
        self.assertIn(report["state"], ("verified", "audit_clean"))
        after = tree(project)
        created = sorted(set(after) - set(before))
        self.assertTrue(any("reminders" in rel for rel in created), created)
        self.assertEqual(evolution.plan_evolution(evolution.load_evolution(str(contract))).state, "noop")
        self.settled(project)
        self.record("03_evolution", state=report["state"], created=len(created), modified=len([k for k in before if after.get(k) != before[k]]), applied=report["applied"]["state"], database=report["applied"]["database"]["status"], noop_on_repeat=True)

    # 4. opt-in merge ------------------------------------------------------------- #

    def test_04_a_hand_edit_on_the_integration_point_is_merged_only_with_the_opt_in(self) -> None:
        project = self.__class__.project
        main = project / MAIN
        text = main.read_text(encoding="utf-8")
        text = text.replace("from fastapi import FastAPI\n", "from fastapi import FastAPI\nfrom starlette.middleware.gzip import GZipMiddleware  # manual: compress large payloads\n")
        text = text.replace("    install_error_handlers(app)\n", "    install_error_handlers(app)\n    app.add_middleware(GZipMiddleware, minimum_size=1024)  # manual: added by the team\n")
        main.write_text(text, encoding="utf-8")
        manual_digest = sha(main)
        add = {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "attachments", "entities": [{"name": "Attachment", "fields": [{"name": "filename", "type": "str", "max_length": 120}], "operations": ["create", "get", "list"]}]}], "invariants": INVARIANTS}
        plain = self.write_json("attachments.json", add)
        blocked = evolution.plan_evolution(evolution.load_evolution(str(plain)))
        self.assertEqual(sorted({b.split(":")[0] for b in blocked.blockers}), ["manual_change_on_integration_point"])
        merge = self.write_json("attachments-merge.json", {**add, "files": {"merge": [{"path": MAIN, "transform": "three_way_python"}]}})
        half = evolution.plan_evolution(evolution.load_evolution(str(merge)))
        self.assertEqual(sorted({b.split(":")[0] for b in half.blockers}), ["merge_not_authorized_on_command_line"], "the contract alone is not the opt-in")
        self.assertEqual(sha(main), manual_digest, "planning wrote nothing")
        report = evolution.run_evolution(str(merge), self.trusted(project, allow_merge=True))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"] or report.get("verification", {}).get("reasons"))
        merged = main.read_text(encoding="utf-8")
        self.assertIn("app.add_middleware(GZipMiddleware, minimum_size=1024)  # manual: added by the team", merged)
        self.assertIn("from starlette.middleware.gzip import GZipMiddleware  # manual: compress large payloads", merged)
        self.assertIn("attachments", merged)
        record = report["plan"]["merges"][0]
        self.assertEqual(record["path"], MAIN)
        self.assertTrue(record["base"]["authenticated"] if isinstance(record.get("base"), dict) and "authenticated" in record["base"] else True)
        self.assertEqual(evolution.plan_evolution(evolution.load_evolution(str(merge)), allow_merge=True).state, "noop")
        self.settled(project)
        self.record("04_merge_opt_in", without_contract="manual_change_on_integration_point", without_flag="merge_not_authorized_on_command_line", merged_state=report["state"], manual_lines_preserved=True, merges=[{k: record.get(k) for k in ("path", "transform", "local_sha256", "new_sha256", "result_sha256")}], noop_on_repeat=True)

    # 5. adoption of a hand-written project ------------------------------------- #

    def test_05_adoption_mounts_a_generated_module_and_extends_an_adopted_router(self) -> None:
        shop = self.tmp / "workshop"
        shutil.copytree(WORKSHOP, shop, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
        before = tree(shop)
        contract, errors = adoption.validate_adoption({"schema": adoption.ADOPTION_SCHEMA_11, "project": {"path": str(shop)}, **WORKSHOP_ADOPTION})
        self.assertEqual(errors, [])
        adopted = adoption.adopt(contract)
        self.assertEqual(adopted["written"], [adoption.ADOPTION])
        reviews = self.write_json("reviews.json", {"schema": SCHEMA, "project": {"path": str(shop)}, "add_modules": [{"name": "reviews", "entities": [{"name": "Review", "fields": [{"name": "author", "type": "str", "max_length": 80}, {"name": "rating", "type": "int", "min_value": 1}], "operations": ["create", "get", "list"]}]}]})
        report = evolution.run_evolution(str(reviews), self.trusted(shop))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"] or report.get("verification", {}).get("reasons"))
        mount = report["plan"]["mount"]
        self.assertEqual(mount["file"], "app/main.py")
        self.assertEqual([r["effective_path"] for r in mount["expected_routes"]], ["/api/reviews", "/api/reviews/{review_id}", "/api/reviews"])
        if report.get("verification"):
            self.assertEqual(report["verification"]["mount"]["unresolved"], [])
        extend = self.write_json("extend.json", {"schema": SCHEMA, "project": {"path": str(shop)}, "modify_modules": [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/reports/stats", "name": "items_stats", "parameters": [{"name": "min_price", "in": "query", "type": "float", "required": False, "default": 0.0}], "delegate": {"callable": "app.services:count_items", "arguments": ["min_price"]}, "response_model": "ItemStats"}]}]})
        report2 = evolution.run_evolution(str(extend), self.trusted(shop))
        self.assertEqual(report2["applied"]["state"], "applied", report2["plan"]["blockers"] or report2.get("verification", {}).get("reasons"))
        after = tree(shop)
        self.assertEqual(after["app/routers/users.py"], before["app/routers/users.py"], "the unadopted router never changes")
        self.assertEqual(after["app/storage.py"], before["app/storage.py"])
        main = (shop / "app" / "main.py").read_text(encoding="utf-8")
        self.assertEqual(main.replace("from app.reviews.router import router as reviews_router\n", "").replace('    application.include_router(reviews_router, prefix="/api/reviews")\n', ""), (WORKSHOP / "app" / "main.py").read_text(encoding="utf-8"), "exactly two lines were added to the hand-written mount file")
        self.assertEqual(evolution.plan_evolution(evolution.load_evolution(str(reviews))).state, "noop")
        self.settled(shop)
        self.__class__.shop = shop
        self.record("05_adoption", adopted_modules=sorted(adopted["adopted"]), mount=mount["steps"], module_state=report["state"], extension_state=report2["state"], guards=sorted(report2["plan"]["guards"]), unadopted_untouched=True)

    # 6. interruption and recovery ------------------------------------------------ #

    def test_06_an_interrupted_publication_is_rolled_back_and_the_retry_applies(self) -> None:
        shop = self.__class__.shop
        before = tree(shop)
        tags = self.write_json("tags.json", {"schema": SCHEMA, "project": {"path": str(shop)}, "add_modules": [{"name": "tags", "entities": [{"name": "Tag", "fields": [{"name": "label", "type": "str", "max_length": 40}], "operations": ["create", "get", "list"]}]}]})
        report = evolution.run_evolution(str(tags), evolution.EvolveOptions(apply=True, verify=False, faults=journal.FaultInjector(points=["after_publish:2"])))
        self.assertEqual(report["applied"]["state"], "interrupted")
        pending = journal.pending_operations(shop)
        self.assertEqual(len(pending), 1)
        classified = journal.classify(shop, pending[0])
        outcome = journal.recover(shop, pending[0], strategy="rollback")
        self.assertEqual(outcome.state, "rolled_back")
        self.assertEqual(tree(shop), before, "the rollback restores every byte, the hand-written mount file included")
        retry = evolution.run_evolution(str(tags), self.trusted(shop))
        self.assertEqual(retry["applied"]["state"], "applied", retry["plan"]["blockers"] or retry.get("verification", {}).get("reasons"))
        self.settled(shop)
        self.record("06_interruption_recovery", interrupted_after=2, classified=classified, rollback=outcome.to_dict(), retry=retry["applied"]["state"])

    # 7. the phased rename and the controlled contract: interfaces and transitions -- #

    def test_07_phased_rename_and_controlled_contract_interfaces(self) -> None:
        project = self.tmp / "parcels"
        shutil.copytree(PARCELS, project, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
        before = tree(project)
        rename = json.loads((EXAMPLES / "evolutions" / "parcels-rename-label.json").read_text())
        rename["project"]["path"] = str(project)
        plan = evolution.plan_evolution(evolution.load_evolution(str(self.write_json("rename.json", rename))))
        self.assertEqual(plan.state, "planned", plan.blockers)
        phases = [p["phase"] for p in plan.migration["phases"]]
        self.assertIn("expand", phases)
        contract = json.loads((EXAMPLES / "evolutions" / "parcels-contract-label.json").read_text())
        contract["project"]["path"] = str(project)
        contract_path = self.write_json("contract.json", contract)
        unknown = evolution.plan_evolution(evolution.load_evolution(str(contract_path)), authorize_contract=True)
        self.assertEqual(sorted({b.split(":")[0] for b in unknown.blockers}), ["contract_operation_known"], "before the expand the rename is not an operation the project knows")
        self.assertEqual(tree(project), before, "plans write nothing")
        expanded = evolution.run_evolution(str(self.tmp / "rename.json"), evolution.EvolveOptions(apply=True, verify=False))  # the files of the expand stage; its revisions run on PostgreSQL in the opt-in modules
        self.assertEqual(expanded["applied"]["state"], "applied", expanded["plan"]["blockers"])
        self.assertEqual((expanded["applied"]["database"]["migration_required"], expanded["applied"]["database"]["applied_by_this_tool"]), (True, False))
        after_expand = tree(project)
        blocked = evolution.plan_evolution(evolution.load_evolution(str(contract_path)), authorize_contract=True)
        self.assertEqual(blocked.state, "blocked")
        names = sorted({b.split(":")[0] for b in blocked.blockers})
        self.assertEqual(names, ["contract_rename_retire_first"], "a contract before the retire is refused by name, before any other precondition is weighed")
        self.assertEqual(tree(project), after_expand, "a refused contract writes nothing")
        self.settled(project)
        self.record("07_phased_rename_and_contract_interfaces", rename_phases=phases, rename_files=len(plan.files), expand_applied=expanded["applied"]["state"], database=expanded["applied"]["database"]["status"], contract_before_expand=["contract_operation_known"], contract_after_expand_blockers=names, note="the real cycle A → expand → B → retire → C → contract → D runs on PostgreSQL in AcceptancePostgresTests and in the dedicated modules")

    # 8. the recovery properties of increment 5 ----------------------------------- #

    def test_08_completed_never_publishes_twice_and_a_recovery_interrupted_twice_still_finishes(self) -> None:
        project = self.tmp / "small"
        project.mkdir()
        for name, text in (("a.txt", "a0\n"), ("b.txt", "b0\n"), ("c.txt", "c0\n")):
            (project / name).write_text(text, encoding="utf-8")
        files = {"a.txt": "a1\n", "b.txt": "b1\n", "c.txt": "c1\n"}
        expected = {k: sha(project / k) for k in files}
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="cleanup", faults=journal.FaultInjector(points=["during_cleanup"]))
        op_dir = next((project / journal.OPS_DIR).iterdir())
        raw = json.loads((op_dir / "journal.json").read_text())
        self.assertEqual(raw["state"], "completed")
        self.assertTrue((op_dir / "new" / "a.txt").is_file(), "a failed cleanup keeps the staging until the next reader finishes it")
        digests_before = {k: sha(project / k) for k in files}
        op = journal.Operation.from_dict(raw)
        report = journal.recover(project, op, strategy="forward")
        self.assertEqual((report.state, report.published, report.restored), ("completed", [], []))
        self.assertEqual({k: sha(project / k) for k in files}, digests_before, "completed publishes nothing a second time")
        self.assertEqual(journal.pending_operations(project), [])
        self.assertFalse(op_dir.exists())
        # a recovery interrupted twice
        for name, text in (("a.txt", "a0\n"), ("b.txt", "b0\n"), ("c.txt", "c0\n")):
            (project / name).write_text(text, encoding="utf-8")
        expected = {k: sha(project / k) for k in files}
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, files, expected, kind="test", description="twice", faults=journal.FaultInjector(points=["after_publish_all"]))
        hits = []
        for attempt in range(2):
            op = journal.pending_operations(project)[0]
            with self.assertRaises(journal.InjectedFailure):
                journal.recover(project, op, strategy="rollback", faults=journal.FaultInjector(points=["recover_after:1"]))
            op = journal.pending_operations(project)[0]
            hits.append(len(op.recovered))
            self.assertEqual(op.state, "recovering_rollback")
        self.assertEqual(hits, [1, 2])
        final = journal.recover(project, journal.pending_operations(project)[0], strategy="rollback")
        self.assertEqual((final.state, sorted(final.restored)), ("rolled_back", ["c.txt"]))
        self.assertEqual({k: (project / k).read_text() for k in files}, {"a.txt": "a0\n", "b.txt": "b0\n", "c.txt": "c0\n"})
        self.settled(project)
        # a reused pid is a stale lock, not a held one
        path = project / journal.OPS_DIR / locking.LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": locking.LOCK_SCHEMA, "pid": os.getpid(), "host": socket.gethostname(), "started_at": "2020-01-01T00:00:00+00:00", "process_started_at": "2020-01-01T00:00:00+00:00", "kind": "evolve", "description": "a process that died long ago", "token": "x"}), encoding="utf-8")
        state = locking.inspect(project)
        self.assertEqual((state["status"], state["pid_reused"]), ("stale", True))
        self.assertTrue(locking.unlock_stale(project)["removed"])
        live = locking.acquire(project, kind="evolve", description="me")
        self.assertEqual((locking.inspect(project)["status"], locking.inspect(project)["pid_reused"]), ("held", False))
        live.release()
        self.record("08_recovery_properties", completed_publishes_twice=False, recovery_interrupted_twice_then_finished=True, pid_reuse_detected=True)

    # 9. evidence ------------------------------------------------------------------ #

    def test_09_the_evidence_names_every_step(self) -> None:
        steps = self.evidence["steps"]
        for key in ("01_design_generation", "02_audit_verification", "03_evolution", "04_merge_opt_in", "05_adoption", "06_interruption_recovery", "07_phased_rename_and_contract_interfaces", "08_recovery_properties"):
            self.assertIn(key, steps)
        self.record("09_evidence", written_to=str(Path(EVIDENCE_DIR) if EVIDENCE_DIR else self.tmp), steps=len(steps) + 1)


try:  # the PostgreSQL flow reuses the retire module's helpers; the module itself skips without the opt-in
    from test_retire_contract_v22_postgresql import ENABLED as PG_ENABLED
    from test_retire_contract_v22_postgresql import RetireContractCase, confirmation_phrase
except Exception:  # noqa: BLE001  (an import problem there must not hide the in-memory flow)
    PG_ENABLED, RetireContractCase, confirmation_phrase = False, unittest.TestCase, None  # type: ignore[misc,assignment]

from _pg_support import pg_available  # noqa: E402


@unittest.skipUnless(PG_ENABLED and pg_available() and shutil.which("uv") and confirmation_phrase is not None, "set FA_DESIGN_PG=1 to run the rename cycle against a throwaway cluster")
class AcceptancePostgresTests(RetireContractCase):  # type: ignore[misc,valid-type]
    TIMINGS = "acceptance-rename-cycle-timings.json"

    def test_01_a_expand_b_retire_c_contract_d(self) -> None:
        started = time.time()
        a_project, b_project, db, url = self.stage_b("accept", rows=300)  # A on a fresh database, B (expand) applied and backfilled
        self.timed("a_expand_b_s", started)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference OR weight IS DISTINCT FROM weight_kg"), "0", "after the backfill the new columns mirror the old ones")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'shipments'::regclass AND NOT tgisinternal"), "2", "one sync trigger per renamed column")
        started = time.time()
        c_project, retire_report = self.stage_c(b_project, url, db, "accept", verify=False)  # C (retire) from B's evidence
        self.timed("retire_c_s", started)
        self.assertEqual(retire_report["applied"]["state"], "applied")
        c_version = {x["column"]: x for x in read_manifest(str(c_project))["phased_changes"]}["label"]["application_version"]
        phrase = confirmation_phrase(read_manifest(str(c_project))["phased_changes"])
        evidence_path = self.root / "accept-evidence.json"
        code, evidence, stderr = self.backfill(c_project, url, "verify", "--application-version", c_version, "--output", str(evidence_path))
        self.assertTrue(evidence["ok"], evidence["problems"])
        contract_path = self.drop_contract(c_project, evidence_path, "accept-contract.json", ignore_sessions=["psql"])
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract_path)))
        self.assertEqual(sorted({x.split(":")[0] for x in plan.blockers}), ["contract_authorized", "contract_irreversible_confirmed"], "the flags are not implied")
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract_path)), authorize_contract=True, confirm_irreversible=phrase)
        self.assertEqual(plan.state, "planned", plan.blockers)
        d_project = self.project_copy(c_project, "accept-d")
        started = time.time()
        report = self.evolve_with(d_project, self.drop_contract(d_project, evidence_path, "accept-contract-d.json", ignore_sessions=["psql"]), None, verify=False, authorize_contract=True, confirm_irreversible=phrase)
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.tool(d_project, url, "alembic", "upgrade", "head")
        self.timed("contract_d_s", started)
        columns = self.sql(db, "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns WHERE table_name = 'shipments'").split(",")
        self.assertNotIn("reference", columns, "the old column is gone")
        self.assertNotIn("weight_kg", columns)
        self.assertIn("label", columns)
        self.assertIn("weight", columns)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'shipments'::regclass AND NOT tgisinternal"), "0", "the sync triggers went with the old columns")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments"), "300")
        self.save("acceptance-rename-cycle.json", {"a": str(a_project), "b": str(b_project), "c": str(c_project), "d": str(d_project), "columns_after_d": columns, "contract_report": report["applied"]})


if __name__ == "__main__":
    unittest.main()
