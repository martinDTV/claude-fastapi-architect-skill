"""Acceptance of v2.1.0: locked rows under the backfill, the contract preconditions on a real database, and the
two end-to-end scenarios (a generated and evolved backend; a phased PostgreSQL migration).

Scenario A runs wherever the generated dependencies import on Python 3.12+ (no database). Everything else needs
``FA_DESIGN_PG=1`` and the PostgreSQL binaries: a throwaway cluster, own fixtures only, nothing of anyone else.
Every number asserted here was measured on that cluster; ``FA_ACCEPTANCE_EVIDENCE=DIR`` keeps the reports.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS
from _pg_support import OWNER, ThrowawayCluster, find_pg, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design import evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
INVENTORY_SPEC = EXAMPLES / "specs" / "inventory-service.json"
INVENTORY_EVOLUTION = EXAMPLES / "evolutions" / "inventory-add-suppliers.json"
EXPAND = EXAMPLES / "evolutions" / "parcels-expand-carrier.json"
CONTRACT = EXAMPLES / "evolutions" / "parcels-contract-carrier.json"
PG_ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVIDENCE_DIR = os.environ.get("FA_ACCEPTANCE_EVIDENCE")
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx", "pydantic_settings"))
PY312 = sys.version_info >= (3, 12)
SUITE = "uv run --group dev pytest -q -p no:cacheprovider"


def _save(name: str, payload: object) -> None:
    if EVIDENCE_DIR:
        Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
        (Path(EVIDENCE_DIR) / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _uv_env() -> Dict[str, str]:
    home = Path.home()
    return {"UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(home / ".cache" / "uv")), "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR", str(home / ".local" / "share" / "uv" / "python"))}


def _digest_tree(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in DEFAULT_WALK_SKIP for part in path.relative_to(root).parts):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
# Scenario A: requirements -> design -> generation -> audit -> tests -> evolution -> verification (no database)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAS_FASTAPI and PY312, "fastapi, pytest, httpx and pydantic-settings on Python >= 3.12 are needed by the generated suite")
class ScenarioAGeneratedAndEvolvedBackend(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-acceptance21-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_scenario_a_is_reproducible_and_never_touches_protected_code(self) -> None:
        started = time.time()
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        # requirements and design
        spec = design.load_spec(str(INVENTORY_SPEC))
        self.assertIsNone(design.generation_refusal(spec))
        rec = design.recommend(spec)
        self.assertEqual(rec.style, "layered")
        self.assertEqual(design.recommend(spec).to_dict(), rec.to_dict())
        # generation twice: identical files, identical manifests (no timestamps anywhere)
        first, second = self.tmp / "one", self.tmp / "two"
        design.run_design(str(INVENTORY_SPEC), design.DesignOptions(target=str(first), audit=False))
        design.run_design(str(INVENTORY_SPEC), design.DesignOptions(target=str(second), audit=False))
        self.assertEqual(_digest_tree(first), _digest_tree(second))
        self.assertEqual(read_manifest(str(first)), read_manifest(str(second)))
        shutil.rmtree(second)
        shutil.rmtree(first)
        # generation with audit and tests through the trust model
        target = self.tmp / "inventory"
        authorization = Authorization(execution=True, trust="fixture", scope=str(target), declared_by="tests: v2.1 acceptance, project generated by this suite")
        policy = ExecutionPolicy(authorized=True, command=command, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH))
        generated = design.run_design(str(INVENTORY_SPEC), design.DesignOptions(target=str(target), authorization=authorization, policy=policy, test_command=command))
        self.assertEqual(generated["generation"]["conflicts"], [])
        audit = generated["verification"]["audit"]
        self.assertLessEqual(set(audit["by_severity"]), {"info"}, audit["findings"])
        run = generated["verification"]["tests"]["run"]
        self.assertEqual(run["outcome"], "passed", (run.get("stdout_tail") or "")[-3000:])
        self.assertEqual((run["counts"]["failed"], run["counts"]["errors"]), (0, 0))
        self.assertTrue(run["evidence"]["matched"] and run["evidence"]["verifiable"])
        self.assertEqual(set(design.verify_manifest(str(target))["status"].values()), {"identical"})
        # protected code: a hand-written file outside the manifest, a hand edit on a managed module file of another
        # module (preserved as a manual change) and the same file listed under files.preserve
        package = read_manifest(str(target))["package"]
        custom = target / "src" / package / "custom_rules.py"
        custom.write_text('"""Hand-written: must never be touched by an evolution."""\n\nRULES = ("keep",)\n', encoding="utf-8")
        edited = f"src/{package}/services/catalog.py"
        (target / edited).write_text((target / edited).read_text(encoding="utf-8") + "\n# operator note: hand-edited after generation\n", encoding="utf-8")
        before = _digest_tree(target)
        # evolution with the published example contract, verified in a copy and applied
        doc = json.loads(INVENTORY_EVOLUTION.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(target)
        doc["files"] = {"preserve": [edited]}
        contract = self.tmp / "evolve.json"
        contract.write_text(json.dumps(doc), encoding="utf-8")
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract)))
        self.assertEqual(plan.state, "planned", (plan.blockers, plan.conflicts))
        evolved = evolution.run_evolution(evolution.load_evolution(str(contract)), evolution.EvolveOptions(apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(target), declared_by="tests: v2.1 acceptance, evolved copy"), policy=policy, test_command=command))
        self.assertEqual(evolved["state"], "verified", (evolved.get("verification") or {}).get("reasons"))
        self.assertEqual(evolved["applied"]["state"], "applied")
        verification = evolved["verification"]
        self.assertEqual(verification["routes"]["missing"], [])
        self.assertGreater(verification["routes"]["after"], verification["routes"]["before"])
        self.assertEqual(verification["protected_files"]["unexpectedly_changed"], [])
        self.assertEqual(verification["tests"]["run"]["counts"]["failed"], 0)
        after = _digest_tree(target)
        self.assertEqual(after[f"src/{package}/custom_rules.py"], before[f"src/{package}/custom_rules.py"])
        self.assertEqual(after[edited], before[edited], "the hand edit on a managed file of another module is preserved byte for byte")
        self.assertNotIn(edited, evolved["plan"]["files"]["modify"])
        self.assertIn(edited, evolved["plan"]["files"]["preserved_manual_changes"])
        model = design.discover(str(target))
        self.assertEqual(model.ownership[f"src/{package}/custom_rules.py"], "unmanaged")
        self.assertEqual(model.ownership[edited], "managed_modified")
        self.assertEqual({v for k, v in model.ownership.items() if k not in (f"src/{package}/custom_rules.py", edited)}, {"managed_unchanged"})
        self.assertEqual([h["operation"] for h in model.manifest["history"]], ["generate", "evolve"])
        again = evolution.run_evolution(evolution.load_evolution(str(contract)), evolution.EvolveOptions(apply=True))
        self.assertEqual(again["state"], "noop")
        self.assertEqual(_digest_tree(target), after)
        _save("acceptance21-scenario-a.json", {"wall_s": round(time.time() - started, 1), "recommendation": rec.to_dict(), "generation_tests": run["counts"], "evolution": {k: evolved[k] for k in ("state", "plan", "verification", "applied")}, "repeat": again["state"], "files": len(after)})


# --------------------------------------------------------------------------- #
# PostgreSQL: locked rows, contract preconditions, scenario B
# --------------------------------------------------------------------------- #


@unittest.skipUnless(PG_ENABLED, "set FA_DESIGN_PG=1 to run the PostgreSQL acceptance of v2.1 on a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class PhasedAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ThrowawayCluster().__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="fa-acceptance21-pg-"))
        cls.timings: Dict[str, float] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)
        _save("acceptance21-timings.json", cls.timings)

    # -- helpers ------------------------------------------------------------- #

    def project_copy(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(PARCELS, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target

    def env(self, project: Path, url: str) -> Dict[str, str]:
        prefix = read_manifest(str(project))["package"].upper()
        env = dict(os.environ, **_uv_env())
        env[f"{prefix}_DATABASE_URL"] = url
        env[f"{prefix}_MIGRATIONS_DATABASE_URL"] = url
        return env

    def tool(self, project: Path, url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["uv", "run", "--group", "dev", *args], cwd=str(project), env=self.env(project, url), capture_output=True, text=True, timeout=600)
        if check:
            self.assertEqual(proc.returncode, 0, f"{' '.join(args)}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        return proc

    def backfill(self, project: Path, url: str, *args: str, check: bool = True) -> tuple:
        proc = self.tool(project, url, "python", "-m", "parcels.ops.backfill", *args, check=False)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        payload = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
        return proc.returncode, payload, proc.stderr

    def run_until_not_locked(self, project: Path, url: str, *args: str) -> tuple:
        """A killed worker's backend releases the advisory lock when the server notices the dead client."""
        for _attempt in range(20):
            code, payload, stderr = self.backfill(project, url, "run", *args, check=False)
            if code != 3:
                return code, payload, stderr
            time.sleep(0.5)
        self.fail("the operation stayed locked")

    def sql(self, db: str, statement: str) -> str:
        return self.cluster.psql(statement, db=db)

    def hold_lock(self, db: str, where: str) -> subprocess.Popen:
        """A second session that holds ``SELECT ... FOR UPDATE`` on rows until ``release`` commits it."""
        psql = find_pg("psql")
        assert psql
        session = subprocess.Popen([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-q"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert session.stdin is not None
        session.stdin.write(f"BEGIN;\nSELECT id FROM shipments WHERE {where} FOR UPDATE;\n")
        session.stdin.flush()
        idle = f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{db}' AND state = 'idle in transaction'"
        deadline = time.time() + 30
        before = getattr(self, "_held", 0)
        while time.time() < deadline and int(self.sql(db, idle)) <= before:
            time.sleep(0.05)
        if session.poll() is not None:
            self.fail(f"the locking session ended: {session.stderr.read() if session.stderr else ''}")
        self.assertGreater(int(self.sql(db, idle)), before, "the locking session did not reach idle in transaction")
        self._held = before + 1
        self.addCleanup(lambda: session.poll() is None and session.kill())
        return session

    def release(self, session: subprocess.Popen) -> None:
        assert session.stdin is not None
        session.stdin.write("COMMIT;\n")
        session.stdin.flush()
        session.stdin.close()
        session.wait(timeout=30)
        self._held = getattr(self, "_held", 1) - 1

    def expanded_project(self, name: str, rows: int, *, verify: bool = False, verification_db: Optional[str] = None) -> tuple:
        """A copy of parcels on its own database with the baseline applied, rows inserted, the expand evolution applied
        and the expand and index revisions upgraded. Returns (project, url, db, op ids by column)."""
        project = self.project_copy(name)
        db = f"{name}_db"
        url = self.cluster.create_database(db)
        self.tool(project, url, "alembic", "upgrade", "head")
        self.sql(db, f"INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'ref-' || g FROM generate_series(1, {rows}) g")
        doc = json.loads(EXPAND.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        contract = self.root / f"{name}-expand.json"
        contract.write_text(json.dumps(doc), encoding="utf-8")
        if verify:
            report = self.evolve(project, contract, verification_db or self.cluster.create_database(f"{name}_verify"))
        else:
            report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.tool(project, url, "alembic", "upgrade", "head")
        ops = {c["column"]: c for c in read_manifest(str(project))["phased_changes"]}
        return project, url, db, ops, report

    def evolve(self, project: Path, contract_path: Path, verification_db: str, *, authorize_contract: bool = False, evidence: Optional[str] = None) -> dict:
        for key, value in _uv_env().items():
            os.environ[key] = value
        os.environ["DATABASE_URL"] = verification_db
        options = evolution.EvolveOptions(
            apply=True,
            verify=True,
            authorization=Authorization(execution=True, trust="fixture", scope=str(project.parent), declared_by="tests: v2.1 acceptance, FA_DESIGN_PG opt-in"),
            policy=ExecutionPolicy(authorized=True, command=SUITE, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + ("DATABASE_URL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")),
            test_command=SUITE,
            authorize_contract=authorize_contract,
            evidence=evidence,
        )
        return evolution.run_evolution(str(contract_path), options)

    def contract_for(self, project: Path, name: str) -> Path:
        doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def progress(self, db: str, op_id: str) -> dict:
        row = self.sql(db, f"SELECT row_to_json(p) FROM parcels_backfill_progress p WHERE op_id = '{op_id}'")
        return json.loads(row)

    def timed(self, label: str, started: float) -> None:
        self.timings[label] = round(time.time() - started, 3)

    # -- Phase 1: locked rows ------------------------------------------------ #

    def test_01_locked_rows_are_skipped_now_filled_later_and_never_omitted(self) -> None:
        project, url, db, ops, _ = self.expanded_project("locks", 600)
        carrier = ops["carrier"]["op_id"]
        first_key = self.sql(db, "SELECT id FROM shipments ORDER BY id LIMIT 1")
        middle_key = self.sql(db, "SELECT id FROM shipments ORDER BY id OFFSET 299 LIMIT 1")
        start_lock = self.hold_lock(db, f"id = '{first_key}'")
        middle_lock = self.hold_lock(db, f"id = '{middle_key}'")
        evidence: Dict[str, object] = {"rows": 600, "locked": {"start": first_key, "middle": middle_key}}

        # 1. both rows locked for the whole run: the runner advances past them, re-counts, retries, and reports honestly
        started = time.time()
        code, report, stderr = self.backfill(project, url, "run", carrier, "--batch-size", "100", check=False)
        self.timed("locked_run_s", started)
        self.assertEqual(code, 6, stderr)
        self.assertEqual((report["state"], report["rows_this_run"], report["batches_this_run"], report["passes_this_run"], report["pending_rows"]), ("incomplete", 598, 6, 3, 2))
        checkpoint = self.progress(db, carrier)
        self.assertGreater(checkpoint["last_key"], first_key, "keys above the locked row were processed and the checkpoint moved past it")
        self.assertEqual(self.sql(db, f"SELECT carrier IS NULL FROM shipments WHERE id = '{first_key}'"), "t")
        self.assertEqual(self.sql(db, f"SELECT carrier IS NULL FROM shipments WHERE id = '{middle_key}'"), "t")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE carrier IS NOT NULL"), "598")
        evidence["locked_run"] = report

        # 2. the first lock released: the row with the lowest key, left behind by a checkpoint far above it, is found
        self.release(start_lock)
        code, report, stderr = self.backfill(project, url, "run", carrier, "--batch-size", "100", check=False)
        self.assertEqual(code, 6, stderr)
        self.assertEqual((report["state"], report["rows_this_run"], report["pending_rows"]), ("incomplete", 1, 1))
        self.assertEqual(self.sql(db, f"SELECT carrier FROM shipments WHERE id = '{first_key}'"), "unknown")
        self.assertEqual(self.sql(db, f"SELECT carrier IS NULL FROM shipments WHERE id = '{middle_key}'"), "t")
        evidence["after_first_release"] = report

        # 3. the second lock released: complete, every row exactly once, counters cumulative, identity unchanged
        self.release(middle_lock)
        code, report, _ = self.backfill(project, url, "run", carrier, "--batch-size", "100")
        self.assertEqual((report["state"], report["rows_this_run"]), ("complete", 1))
        final = self.progress(db, carrier)
        self.assertEqual((final["state"], final["rows_processed"], final["op_id"]), ("complete", 600, carrier))
        self.assertEqual(final["passes"], checkpoint["passes"] + 3 + report["passes_this_run"], "passes accumulate across runs: three, three more, then this run's")
        self.assertIsNotNone(final["finished_at"])
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE carrier IS NULL"), "0")
        evidence["after_second_release"] = report

        # 4. rows already filled are never touched again: a further run processes nothing and changes no value
        fingerprint = "SELECT md5(string_agg(id::text || ':' || carrier, ',' ORDER BY id)) FROM shipments"
        before = self.sql(db, fingerprint)
        code, report, _ = self.backfill(project, url, "run", carrier)
        self.assertEqual((report["state"], report["rows_this_run"]), ("complete", 0))
        self.assertIn("zero pending rows re-counted", report["note"])
        self.assertEqual(self.sql(db, fingerprint), before)

        # 5. rows that appear after completion reopen the operation for exactly them; history and identity preserved
        self.sql(db, "INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'late-' || g FROM generate_series(1, 5) g")
        code, report, _ = self.backfill(project, url, "run", carrier, "--batch-size", "100")
        self.assertEqual((report["state"], report["rows_this_run"]), ("complete", 5))
        self.assertIn("appeared after completion: reopened", report["note"])
        reopened = self.progress(db, carrier)
        self.assertEqual((reopened["op_id"], reopened["rows_processed"], reopened["state"]), (carrier, 605, "complete"))
        self.assertGreater(reopened["passes"], final["passes"])
        self.assertGreater(reopened["batches"], final["batches"])
        self.assertEqual(self.sql(db, "SELECT md5(string_agg(id::text || ':' || carrier, ',' ORDER BY id)) FROM shipments WHERE reference LIKE 'ref-%'"), before, "the 600 original rows kept the values of the first completion")
        self.backfill(project, url, "run", ops["tracking_token"]["op_id"])  # the second operation, so the evidence can be ok
        code, evidence_doc, _ = self.backfill(project, url, "verify")
        self.assertTrue(evidence_doc["ok"], evidence_doc["problems"])
        self.assertEqual(evidence_doc["operations"][carrier]["progress"]["rows_processed"], 605)
        evidence["reopened"] = report
        _save("acceptance21-locked-rows.json", evidence)

    # -- Phase 2: contract preconditions on the real database -------------- #

    def test_02_contract_preconditions_separate_declaration_observation_and_automation(self) -> None:
        project, url, db, ops, _ = self.expanded_project("contract", 200)
        carrier, token = ops["carrier"]["op_id"], ops["tracking_token"]["op_id"]
        self.backfill(project, url, "run", carrier)
        self.backfill(project, url, "run", token)
        version = ops["carrier"]["application_version"]
        evidence_path = self.root / "contract-evidence.json"

        def fresh_evidence() -> dict:
            code, doc, _ = self.backfill(project, url, "verify", "--application-version", version, "--output", str(evidence_path))
            return doc

        def plan_contract() -> evolution.EvolutionPlan:
            return evolution.plan_evolution(evolution.load_evolution(str(self.contract_for(project, "contract.json"))), authorize_contract=True, evidence_path=str(evidence_path))

        doc = fresh_evidence()
        self.assertTrue(doc["ok"], doc["problems"])
        plan = plan_contract()
        self.assertEqual(plan.state, "planned", plan.blockers)
        checks = {p.name: p for p in plan.preconditions if p.name.startswith("contract_")}
        declared_by_operator = {"contract_authorized", "contract_application_version_accredited"}
        observed_in_database = {"contract_database_at_head", "contract_column_present", "contract_backfill_complete", "contract_no_pending_rows", "contract_indexes_valid"}
        automated_on_documents = {"contract_evidence_present", "contract_evidence_fresh", "contract_evidence_matches", "contract_operation_pending", "contract_reversible"}
        self.assertEqual(set(checks), declared_by_operator | observed_in_database | automated_on_documents)
        self.assertIn("operator declared application version", checks["contract_application_version_accredited"].detail)
        self.assertIn("declared by", checks["contract_authorized"].detail)
        record: Dict[str, object] = {"all_preconditions": [p.to_dict() for p in plan.preconditions if p.name.startswith("contract_")], "categories": {"declared_by_operator": sorted(declared_by_operator), "observed_in_database_via_evidence": sorted(observed_in_database), "automated_on_documents": sorted(automated_on_documents)}, "blocked_by": {}}

        def expect_blocked(label: str, name: str) -> None:
            blocked = plan_contract()
            self.assertEqual(blocked.state, "blocked", label)
            self.assertTrue(any(b.startswith(name) for b in blocked.blockers), (label, blocked.blockers))
            record["blocked_by"][label] = [b for b in blocked.blockers if b.startswith("contract_")]

        # a NULL that reappeared: the evidence taken now says so, the plan blocks; a re-run reopens and fills it
        self.sql(db, "UPDATE shipments SET carrier = NULL WHERE reference = 'ref-1'")
        self.assertFalse(fresh_evidence()["ok"])
        expect_blocked("null_reappeared", "contract_no_pending_rows")
        code, report, _ = self.backfill(project, url, "run", carrier)
        self.assertEqual((report["state"], report["rows_this_run"]), ("complete", 1))
        self.assertTrue(fresh_evidence()["ok"])
        # stale evidence
        stale = json.loads(evidence_path.read_text(encoding="utf-8"))
        stale["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        evidence_path.write_text(json.dumps(stale), encoding="utf-8")
        expect_blocked("stale_evidence", "contract_evidence_fresh")
        # the database at another revision than the manifest head (the index revision not applied)
        self.tool(project, url, "alembic", "downgrade", "-1")
        self.assertFalse(fresh_evidence()["alembic"]["matches_expected_head"])
        expect_blocked("database_below_head", "contract_database_at_head")
        self.assertTrue(any("missing: ['ix_shipments_carrier']" in b for b in record["blocked_by"]["database_below_head"]), "an index the database does not hold is reported missing, not invalid")
        self.tool(project, url, "alembic", "upgrade", "head")
        # an unexpected revision recorded in the database
        head = self.sql(db, "SELECT version_num FROM alembic_version")
        self.sql(db, "UPDATE alembic_version SET version_num = 'ffffffffffff'")
        self.assertFalse(fresh_evidence()["alembic"]["matches_expected_head"])
        expect_blocked("unexpected_revision", "contract_database_at_head")
        self.sql(db, f"UPDATE alembic_version SET version_num = '{head}'")
        # an invalid index
        self.sql(db, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'ix_shipments_carrier'::regclass")
        self.assertFalse(fresh_evidence()["ok"])
        expect_blocked("invalid_index", "contract_indexes_valid")
        self.sql(db, "UPDATE pg_index SET indisvalid = true WHERE indexrelid = 'ix_shipments_carrier'::regclass")
        self.assertTrue(fresh_evidence()["ok"])
        # evidence that was true when taken and is not any more: the plan trusts the document, the revision does not
        self.sql(db, "UPDATE shipments SET carrier = NULL WHERE reference = 'ref-2'")
        self.assertEqual(plan_contract().state, "planned", "the plan reads the document the operator handed it")
        started = time.time()
        applied = self.evolve(project, self.contract_for(project, "contract.json"), self.cluster.create_database("contract_verify"), authorize_contract=True, evidence=str(evidence_path))
        self.timed("contract_evolution_with_outdated_evidence_s", started)
        self.assertEqual(applied["state"], "verified", (applied.get("verification") or {}).get("reasons") or applied["plan"]["blockers"])
        proc = self.tool(project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0, "the contract revision re-checks the live database and refuses")
        self.assertIn("still NULL", proc.stderr + proc.stdout)
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "YES")
        after_refusal = fresh_evidence()
        self.assertFalse(after_refusal["ok"])
        self.assertTrue(any("declared contracted but the column is still nullable" in p for p in after_refusal["problems"]), after_refusal["problems"])
        record["declared_but_not_applied"] = after_refusal["problems"]
        # the row fixed: the revision applies; both columns NOT NULL; the transitional constraint is gone
        self.sql(db, "UPDATE shipments SET carrier = 'unknown' WHERE reference = 'ref-2'")
        self.tool(project, url, "alembic", "upgrade", "head")
        for column in ("carrier", "tracking_token"):
            self.assertEqual(self.sql(db, f"SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = '{column}'"), "NO")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_shipments_%_not_null'"), "0")
        self.assertTrue(fresh_evidence()["ok"])

        # the race between the last count and the DDL, with the statements the revision uses: PostgreSQL closes it
        self.sql(db, "CREATE TABLE race (id int PRIMARY KEY, c text)")
        self.sql(db, "INSERT INTO race VALUES (1, NULL)")  # a NULL inserted after the count, before ADD CONSTRAINT
        self.sql(db, "ALTER TABLE race ADD CONSTRAINT ck_race_c_not_null CHECK (c IS NOT NULL) NOT VALID")
        with self.assertRaises(subprocess.CalledProcessError) as rejected:
            self.sql(db, "INSERT INTO race VALUES (2, NULL)")  # once the constraint exists, no NULL enters
        self.assertIn("ck_race_c_not_null", rejected.exception.stderr)
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            self.sql(db, "ALTER TABLE race VALIDATE CONSTRAINT ck_race_c_not_null")  # the pre-existing NULL fails validation
        self.assertIn("ck_race_c_not_null", failed.exception.stderr)
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'race' AND column_name = 'c'"), "YES")
        self.sql(db, "UPDATE race SET c = 'x' WHERE id = 1")
        self.sql(db, "ALTER TABLE race VALIDATE CONSTRAINT ck_race_c_not_null")
        self.sql(db, "ALTER TABLE race ALTER COLUMN c SET NOT NULL")
        record["race"] = {"insert_after_not_valid_constraint": "rejected (23514)", "validate_with_pre_existing_null": "fails, column stays nullable", "after_fix": "validate and SET NOT NULL succeed"}
        _save("acceptance21-contract-preconditions.json", record)

    # -- Scenario B: the phased migration end to end --------------------------- #

    def test_03_scenario_b_phased_migration_end_to_end(self) -> None:
        started_all = time.time()
        started = time.time()
        project, url, db, ops, expand_report = self.expanded_project("scenario_b", 1000, verify=True)
        self.timed("b_expand_verified_applied_s", started)
        self.assertEqual(expand_report["state"], "verified", (expand_report.get("verification") or {}).get("reasons"))
        self.assertEqual(expand_report["verification"]["tests"]["run"]["counts"]["failed"], 0)
        carrier, token = ops["carrier"]["op_id"], ops["tracking_token"]["op_id"]
        self.assertEqual(self.sql(db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_shipments_carrier'::regclass"), "t")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM alembic_version"), "1")
        # an interrupted backfill: the runner killed mid-run; checkpoint and rows always agree
        venv_python = project / ".venv" / "bin" / "python"
        self.assertTrue(venv_python.is_file())
        killed = subprocess.Popen([str(venv_python), "-m", "parcels.ops.backfill", "run", carrier, "--batch-size", "50", "--batch-pause-ms", "200"], cwd=str(project), env=self.env(project, url), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            row = self.sql(db, f"SELECT coalesce(rows_processed, 0) FROM parcels_backfill_progress WHERE op_id = '{carrier}'") or "0"
            if int(row) >= 300:
                break
            time.sleep(0.05)
        killed.send_signal(signal.SIGKILL)
        killed.communicate(timeout=60)
        processed = int(self.sql(db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{carrier}'"))
        filled = int(self.sql(db, "SELECT count(*) FROM shipments WHERE carrier IS NOT NULL"))
        self.assertEqual(processed, filled, "checkpoint and data commit together")
        self.assertLess(filled, 1000)
        # resume, then the second operation
        started = time.time()
        code, resumed, stderr = self.run_until_not_locked(project, url, carrier, "--batch-size", "200")
        self.timed("b_resume_s", started)
        self.assertEqual(code, 0, stderr)
        self.assertEqual((resumed["state"], resumed["rows_processed"]), ("complete", 1000))
        self.assertEqual(self.sql(db, "SELECT count(DISTINCT carrier) FROM shipments"), "1")
        code, token_run, _ = self.backfill(project, url, "run", token, "--batch-size", "250")
        self.assertEqual((token_run["state"], token_run["rows_this_run"], token_run["batches_this_run"]), ("complete", 1000, 4))
        # new records after complete: both operations reopen for exactly them
        self.sql(db, "INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'new-' || g FROM generate_series(1, 7) g")
        for op_id in (carrier, token):
            code, reopened, _ = self.backfill(project, url, "run", op_id)
            self.assertEqual((reopened["state"], reopened["rows_this_run"]), ("complete", 7), op_id)
            self.assertIn("reopened", reopened["note"])
        fingerprint = "SELECT md5(string_agg(id::text || ':' || carrier || ':' || tracking_token::text, ',' ORDER BY id)) FROM shipments"
        values_before_contract = self.sql(db, fingerprint)
        # verify, then the authorised contract
        evidence_path = self.root / "scenario-b-evidence.json"
        code, evidence, _ = self.backfill(project, url, "verify", "--application-version", ops["carrier"]["application_version"], "--output", str(evidence_path))
        self.assertTrue(evidence["ok"], evidence["problems"])
        self.assertEqual({o["progress"]["rows_processed"] for o in evidence["operations"].values()}, {1007})
        self.assertTrue(evidence["indexes"][0]["valid"])
        started = time.time()
        contract_report = self.evolve(project, self.contract_for(project, "scenario-b-contract.json"), self.cluster.create_database("scenario_b_contract_verify"), authorize_contract=True, evidence=str(evidence_path))
        self.timed("b_contract_verified_applied_s", started)
        self.assertEqual(contract_report["state"], "verified", (contract_report.get("verification") or {}).get("reasons") or contract_report["plan"]["blockers"])
        self.assertTrue(all(p["ok"] for p in contract_report["plan"]["preconditions"]), [p for p in contract_report["plan"]["preconditions"] if not p["ok"]])
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("b_contract_upgrade_s", started)
        # invariants and final state
        for column in ("carrier", "tracking_token"):
            self.assertEqual(self.sql(db, f"SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = '{column}'"), "NO")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments"), "1007")
        self.assertEqual(self.sql(db, fingerprint), values_before_contract, "the contract changed no value")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_shipments_%_not_null'"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM alembic_version"), "1")
        code, final_evidence, _ = self.backfill(project, url, "verify")
        self.assertTrue(final_evidence["ok"], final_evidence["problems"])
        manifest = read_manifest(str(project))
        self.assertEqual({c["state_declared"] for c in manifest["phased_changes"]}, {"contract"})
        self.assertEqual([m["phase"] for m in manifest["migrations"]], ["initial", "expand", "index", "contract"])
        self.assertEqual(set(design.verify_manifest(str(project))["status"].values()), {"identical"})
        model = design.discover(str(project), audit=False)
        self.assertEqual((len(model.alembic["revisions"]), model.alembic["historical_modified"]), (4, []))
        with self.assertRaises(subprocess.CalledProcessError):
            self.sql(db, "INSERT INTO shipments (id, reference) VALUES (gen_random_uuid(), 'null-carrier')")  # NOT NULL holds
        self.timed("b_total_s", started_all)
        _save("acceptance21-scenario-b.json", {"expand": {k: expand_report[k] for k in ("state", "applied")}, "expand_tests": expand_report["verification"]["tests"]["run"]["counts"], "killed_at": processed, "resumed": resumed, "token_run": token_run, "evidence": evidence, "contract": {k: contract_report[k] for k in ("state", "applied")}, "contract_tests": contract_report["verification"]["tests"]["run"]["counts"], "manifest_phases": [m["phase"] for m in manifest["migrations"]]})


if __name__ == "__main__":
    unittest.main()
