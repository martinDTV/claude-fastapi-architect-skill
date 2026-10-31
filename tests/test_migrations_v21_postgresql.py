"""Opt-in (``FA_DESIGN_PG=1``): phased migrations against a real, throwaway PostgreSQL (v2.1, increment 2).

The vertical slice on the fifth example (``parcels``): a baseline with 2 500 existing rows; the expand evolution
(two nullable columns, the progress table, a concurrent index in its own revision) applied with its suite verified
on a separate empty database; the generated runner filling the rows in keyset batches; a clean stop after a batch
limit; a SIGKILL in the middle of a run and the resumption without a duplicated or skipped row; two workers on the
same operation; application writes during the backfill (inserts with NULL by an old version, updates by the new
one) and what the runner guarantees about them; corrupt and foreign checkpoints; the evidence written by
``verify``; a contract refused without authorization, planned with fresh evidence, applied online (NOT VALID,
VALIDATE, SET NOT NULL), refused by the revision itself when a NULL reappears, reversible by downgrade; a
conflicting index definition refused and an invalid index rebuilt; a connection failure and a time limit; the
runner refusing a row-level-security table as the application role. Every number asserted here was measured on
this cluster; nothing is inferred.

Needs the PostgreSQL binaries (``initdb``, ``pg_ctl``, ``psql``) and ``uv`` (the generated project is synced from the
local cache). Everything executed runs through the trust model or through ``uv run`` inside temporary copies.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS
from _pg_support import ThrowawayCluster, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design.evolution import EvolveOptions, load_evolution, plan_evolution, run_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
TENANT_PROJECTS = EXAMPLES / "generated" / "tenant-projects"
EXPAND = EXAMPLES / "evolutions" / "parcels-expand-carrier.json"
CONTRACT = EXAMPLES / "evolutions" / "parcels-contract-carrier.json"
ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVIDENCE_DIR = os.environ.get("FA_DESIGN_PG_EVIDENCE")
ROWS = 2500
SUITE = "uv run --group dev pytest -q -p no:cacheprovider"


def _uv_env() -> Dict[str, str]:
    home = Path.home()
    return {"UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(home / ".cache" / "uv")), "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR", str(home / ".local" / "share" / "uv" / "python"))}


@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the phased migrations against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class PhasedMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ThrowawayCluster().__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="fa-phased-pg-"))
        cls.timings: Dict[str, float] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / "phased-timings.json").write_text(json.dumps(cls.timings, indent=2, sort_keys=True), encoding="utf-8")

    # -- helpers ------------------------------------------------------------- #

    def project_copy(self, source: Path, name: str) -> Path:
        target = self.root / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target

    def env(self, project: Path, url: str, **extra: str) -> Dict[str, str]:
        prefix = read_manifest(str(project))["package"].upper()
        env = dict(os.environ, **_uv_env())
        env[f"{prefix}_DATABASE_URL"] = url
        env[f"{prefix}_MIGRATIONS_DATABASE_URL"] = url
        env.update(extra)
        return env

    def tool(self, project: Path, url: str, *args: str, timeout: float = 600.0, check: bool = True, **extra: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(["uv", "run", "--group", "dev", *args], cwd=str(project), env=self.env(project, url, **extra), capture_output=True, text=True, timeout=timeout)
        if check:
            self.assertEqual(proc.returncode, 0, f"{' '.join(args)}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        return proc

    def backfill(self, project: Path, url: str, *args: str, check: bool = True, **extra: str) -> tuple:
        package = read_manifest(str(project))["package"]
        proc = self.tool(project, url, "python", "-m", f"{package}.ops.backfill", *args, check=False, **extra)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        payload = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
        return proc.returncode, payload, proc.stderr

    def sql(self, db: str, statement: str) -> str:
        return self.cluster.psql(statement, db=db)

    def evolve(self, project: Path, contract_path: Path, verification_db: str, *, apply: bool = True, authorize_contract: bool = False, evidence: Optional[str] = None) -> dict:
        for key, value in _uv_env().items():
            os.environ[key] = value
        os.environ["DATABASE_URL"] = verification_db
        options = EvolveOptions(
            apply=apply,
            verify=True,
            authorization=Authorization(execution=True, trust="fixture", scope=str(project.parent), declared_by="tests: FA_DESIGN_PG opt-in"),
            policy=ExecutionPolicy(authorized=True, command=SUITE, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + ("DATABASE_URL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")),
            test_command=SUITE,
            authorize_contract=authorize_contract,
            evidence=evidence,
        )
        return run_evolution(str(contract_path), options)

    def write_contract(self, source: Path, project: Path, name: str, **changes) -> Path:
        doc = json.loads(source.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        for key, value in changes.items():
            doc[key] = value
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def save(self, name: str, payload: object) -> None:
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def timed(self, label: str, started: float) -> None:
        self.timings[label] = round(time.time() - started, 3)

    # -- the vertical slice -------------------------------------------------- #

    def test_01_vertical_slice_expand_backfill_verify_contract(self) -> None:
        project = self.project_copy(PARCELS, "parcels")
        prod = self.cluster.create_database("parcels_prod")
        prod_db = "parcels_prod"
        started = time.time()
        self.tool(project, prod, "alembic", "upgrade", "head")
        self.timed("baseline_upgrade_s", started)
        self.sql(prod_db, f"INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'ref-' || g FROM generate_series(1, {ROWS}) g")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments"), str(ROWS))

        # 2. expand (verified on a separate empty database through the trust model)
        verify_db = self.cluster.create_database("parcels_verify_expand")
        started = time.time()
        report = self.evolve(project, self.write_contract(EXPAND, project, "expand.json"), verify_db)
        self.timed("expand_evolution_verified_and_applied_s", started)
        self.save("parcels-expand.json", report)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons") or report["plan"]["blockers"])
        self.assertEqual(report["applied"]["state"], "applied")
        run = report["verification"]["tests"]["run"]
        self.assertEqual(run["counts"]["failed"], 0)
        passed = run["evidence"]["tests"]["passed"]
        for name in ("test_backfill_completes_on_an_empty_database", "test_backfill_fills_existing_rows_in_batches_and_is_idempotent", "test_a_partial_run_resumes_from_its_checkpoint_without_duplication", "test_a_checkpoint_of_another_definition_is_refused", "test_verify_reports_the_state_of_this_database", "test_upgrade_and_downgrade_are_repeatable", "test_migrated_schema_matches_the_models"):
            self.assertTrue(any(p.endswith(name) for p in passed), name)
        manifest = read_manifest(str(project))
        ops = {c["column"]: c for c in manifest["phased_changes"]}
        carrier, token = ops["carrier"]["op_id"], ops["tracking_token"]["op_id"]
        self.assertEqual((carrier, token), ("66636e4277641692", "5821fb0e87d4b355"))
        phases = report["plan"]["migration"]["phases"]
        self.assertEqual([p["phase"] for p in phases], ["expand", "index"])

        # 3. the production database applies expand + index; existing rows read NULL; the index is valid
        started = time.time()
        self.tool(project, prod, "alembic", "upgrade", "head")
        self.timed("expand_and_index_upgrade_s", started)
        self.assertEqual(self.sql(prod_db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "YES")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE carrier IS NULL"), str(ROWS))
        self.assertEqual(self.sql(prod_db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_shipments_carrier'::regclass"), "t")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM parcels_backfill_progress"), "0")

        # 4. backfill in batches: a clean stop after three batches, then two workers, then a SIGKILL mid-run, then completion
        code, partial, _ = self.backfill(project, prod, "run", carrier, "--batch-size", "200", "--max-batches", "3")
        self.assertEqual((partial["state"], partial["rows_this_run"], partial["batches_this_run"]), ("running", 600, 3))
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE carrier IS NOT NULL"), "600")
        self.assertEqual(self.sql(prod_db, f"SELECT rows_processed || ':' || batches || ':' || state FROM parcels_backfill_progress WHERE op_id = '{carrier}'"), "600:3:running")
        env = self.env(project, prod)
        workers = [subprocess.Popen(["uv", "run", "--group", "dev", "python", "-m", "parcels.ops.backfill", "run", carrier, "--batch-size", "50", "--max-batches", "4", "--batch-pause-ms", "150"], cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [w.communicate(timeout=300) for w in workers]
        codes = sorted(w.returncode for w in workers)
        self.assertEqual(codes, [0, 3], [r[1][-300:] for r in results])
        filled = int(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE carrier IS NOT NULL"))
        self.assertEqual(filled, 800, "exactly one worker made progress: 600 + 4 x 50")
        self.assertEqual(self.sql(prod_db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{carrier}'"), "800")
        # the interpreter of the synced environment directly: a SIGKILL to `uv run` would leave the runner alive as an orphan
        venv_python = project / ".venv" / "bin" / "python"
        self.assertTrue(venv_python.is_file(), "uv run synced the project environment in the earlier steps")
        killed = subprocess.Popen([str(venv_python), "-m", "parcels.ops.backfill", "run", carrier, "--batch-size", "50", "--batch-pause-ms", "200"], cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # 50 rows every 200 ms: from 800 to 2 500 takes about 7 s, so the kill lands in the middle of the run
        deadline = time.time() + 120
        while time.time() < deadline:
            done = int(self.sql(prod_db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{carrier}'"))
            if done >= 1000:
                break
            time.sleep(0.05)
        killed.send_signal(signal.SIGKILL)
        killed.communicate(timeout=60)
        rows_processed = int(self.sql(prod_db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{carrier}'"))
        filled = int(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE carrier IS NOT NULL"))
        self.assertEqual(rows_processed, filled, "the checkpoint and the rows commit together: a kill never leaves them apart")
        self.assertLess(filled, ROWS)
        started = time.time()
        for _attempt in range(20):  # the killed worker's backend releases the advisory lock when the server notices the dead client
            code, final, stderr = self.backfill(project, prod, "run", carrier, "--batch-size", "200", check=False)
            if code != 3:
                break
            time.sleep(0.5)
        self.timed("carrier_backfill_resume_to_completion_s", started)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(final["state"], "complete")
        self.assertEqual(final["rows_processed"], ROWS, "every row filled exactly once across stop, two workers, a kill and the resumption")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE carrier IS NULL"), "0")
        self.assertEqual(self.sql(prod_db, "SELECT count(DISTINCT carrier) FROM shipments"), "1")
        code, again, _ = self.backfill(project, prod, "run", carrier)
        self.assertEqual((again["state"], again.get("rows_this_run", 0)), ("complete", 0))

        # 5. application writes during the tracking_token backfill: an old version inserts NULL rows, the new one writes values
        token_values = self.sql(prod_db, "UPDATE shipments SET tracking_token = gen_random_uuid() WHERE reference IN ('ref-1', 'ref-2', 'ref-3') RETURNING id || '=' || tracking_token")
        app_written = dict(line.split("=", 1) for line in token_values.splitlines() if "=" in line)  # psql appends the command tag
        self.assertEqual(len(app_written), 3)
        stop = threading.Event()
        inserted: List[str] = []

        def old_application() -> None:
            n = 0
            while not stop.is_set() and n < 40:
                inserted.append(self.sql(prod_db, f"INSERT INTO shipments (id, reference) VALUES (gen_random_uuid(), 'late-{n}') RETURNING id"))
                n += 1
                time.sleep(0.02)

        writer = threading.Thread(target=old_application)
        writer.start()
        started = time.time()
        code, concurrent, stderr = self.backfill(project, prod, "run", token, "--batch-size", "150", "--batch-pause-ms", "10", check=False)
        writer.join(timeout=60)
        self.timed("tracking_backfill_with_concurrent_writes_s", started)
        self.assertIn(code, (0, 6), stderr[-500:])
        for shipment_id, value in app_written.items():
            self.assertEqual(self.sql(prod_db, f"SELECT tracking_token FROM shipments WHERE id = '{shipment_id}'"), value, "a value the application wrote is never overwritten")
        late_nulls = int(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE tracking_token IS NULL"))
        self.save("parcels-concurrent-backfill.json", {"report": concurrent, "inserted_during_run": len(inserted), "app_written": app_written, "exit_code": code, "null_rows_after_the_run": late_nulls})
        self.assertEqual(concurrent["state"], "incomplete" if code == 6 else "complete")
        # `complete` is a count of zero pending rows at a time, not a promise about rows an old application version inserts
        # afterwards (the contract revision refuses those); with the old version stopped, a re-run finishes the job
        if code == 6 or late_nulls:
            code, concurrent, _ = self.backfill(project, prod, "run", token, "--batch-size", "150")
            self.assertEqual(concurrent["rows_this_run"], late_nulls, "the re-run fills exactly the rows that appeared, nothing twice")
        self.assertEqual(concurrent["state"], "complete")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments WHERE tracking_token IS NULL"), "0")
        total = int(self.sql(prod_db, "SELECT count(*) FROM shipments"))
        self.assertEqual(total, ROWS + len(inserted))
        self.assertEqual(int(self.sql(prod_db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{token}'")), total - len(app_written), "the runner filled every row except the ones the application wrote")

        # 6. checkpoints that cannot be trusted
        self.sql(prod_db, f"UPDATE parcels_backfill_progress SET state = 'running', last_key = 'garbage' WHERE op_id = '{carrier}'")
        code, _, stderr = self.backfill(project, prod, "run", carrier, check=False)
        self.assertEqual(code, 4, stderr)
        self.assertIn("corrupt", stderr)
        self.sql(prod_db, f"UPDATE parcels_backfill_progress SET last_key = NULL, checksum = 'stale' WHERE op_id = '{carrier}'")
        code, _, stderr = self.backfill(project, prod, "run", carrier, check=False)
        self.assertEqual(code, 4, stderr)
        self.assertIn("another definition", stderr)
        code, _, stderr = self.backfill(project, prod, "run", "0123456789abcdef", check=False)
        self.assertEqual(code, 7)
        definition_checksum = json.loads(self.tool(project, prod, "python", "-c", "import json; from parcels.ops import backfill, backfills; print(json.dumps(backfill.checksum_of(backfills.BACKFILLS['" + carrier + "'])))").stdout)
        self.sql(prod_db, f"UPDATE parcels_backfill_progress SET checksum = '{definition_checksum}', state = 'complete' WHERE op_id = '{carrier}'")
        code, reopened, stderr = self.backfill(project, prod, "run", carrier, "--max-seconds", "0.001", check=False)
        self.assertEqual(code, 0, stderr)
        # the old application also left `carrier` NULL in the rows it inserted: the complete operation is reopened for
        # exactly those rows (the time limit stops this run before its first batch), and the next run fills them
        self.assertEqual((reopened["state"], reopened["rows_this_run"]), ("running", 0))
        code, carrier_final, _ = self.backfill(project, prod, "run", carrier, "--batch-size", "100")
        self.assertEqual((carrier_final["state"], carrier_final["rows_this_run"], carrier_final["rows_processed"]), ("complete", len(inserted), total))

        # 7. evidence: the contract is refused without authorization or evidence, planned with both, applied online, reversible
        code, evidence, _ = self.backfill(project, prod, "verify", "--application-version", ops["carrier"]["application_version"], "--output", str(self.root / "evidence.json"))
        self.assertTrue(evidence["ok"], evidence["problems"])
        self.assertTrue(all(o["backfill_complete"] for o in evidence["operations"].values()))
        self.assertTrue(evidence["indexes"][0]["valid"])
        self.save("parcels-evidence.json", evidence)
        contract_path = self.write_contract(CONTRACT, project, "contract.json")
        shutil.copy(EXAMPLES / "evolutions" / "parcels-migration-evidence.sample.json", self.root / "parcels-migration-evidence.sample.json")  # the contract names it relative to itself
        blocked = plan_evolution(load_evolution(str(contract_path)))
        self.assertEqual(blocked.state, "blocked")
        self.assertTrue(any(b.startswith("contract_authorized") for b in blocked.blockers), blocked.blockers)
        stale = plan_evolution(load_evolution(str(contract_path)), authorize_contract=True)
        self.assertTrue(any(b.startswith("contract_evidence_fresh") for b in stale.blockers), f"the committed sample evidence is stale by design: {stale.blockers}")
        verify_db2 = self.cluster.create_database("parcels_verify_contract")
        started = time.time()
        contract_report = self.evolve(project, contract_path, verify_db2, authorize_contract=True, evidence=str(self.root / "evidence.json"))
        self.timed("contract_evolution_verified_and_applied_s", started)
        self.save("parcels-contract.json", contract_report)
        self.assertEqual(contract_report["state"], "verified", contract_report.get("verification", {}).get("reasons") or contract_report["plan"]["blockers"])
        self.assertEqual(contract_report["applied"]["state"], "applied")
        # a NULL that reappears (an old application version) makes the contract revision refuse on the live database
        self.sql(prod_db, "UPDATE shipments SET carrier = NULL WHERE reference = 'ref-7'")
        proc = self.tool(project, prod, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("still NULL", proc.stderr + proc.stdout)
        self.assertEqual(self.sql(prod_db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "YES")
        self.sql(prod_db, "UPDATE shipments SET carrier = 'unknown' WHERE reference = 'ref-7'")
        started = time.time()
        self.tool(project, prod, "alembic", "upgrade", "head")
        self.timed("contract_upgrade_s", started)
        for column in ("carrier", "tracking_token"):
            self.assertEqual(self.sql(prod_db, f"SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = '{column}'"), "NO")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck_shipments_%_not_null'"), "0")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM alembic_version"), "1")
        self.tool(project, prod, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql(prod_db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "YES")
        self.assertEqual(self.sql(prod_db, "SELECT count(*) FROM shipments"), str(total), "downgrade of the contract phase loses no data")
        self.tool(project, prod, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(prod_db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "NO")
        code, final_evidence, _ = self.backfill(project, prod, "verify")
        self.assertTrue(final_evidence["ok"], final_evidence["problems"])
        self.assertFalse(any(o["nullable"] for o in final_evidence["operations"].values()))
        model = design.discover(str(project), audit=False)
        self.assertEqual(len(model.alembic["revisions"]), 4)
        self.assertEqual(model.alembic["historical_modified"], [])
        # 8. a fresh environment applies the whole chain: the contract revision records the empty table, the evidence is
        # ok, and a backfill run has nothing to do (no refusal of the record the revision wrote)
        fresh = self.cluster.create_database("parcels_fresh")
        started = time.time()
        self.tool(project, fresh, "alembic", "upgrade", "head")
        self.timed("fresh_environment_full_chain_upgrade_s", started)
        self.assertEqual(self.sql("parcels_fresh", "SELECT count(*) FROM alembic_version"), "1")
        self.assertEqual(self.sql("parcels_fresh", "SELECT string_agg(checksum, ',' ORDER BY op_id) FROM parcels_backfill_progress"), "empty-table-at-contract,empty-table-at-contract")
        code, fresh_evidence, _ = self.backfill(project, fresh, "verify")
        self.assertTrue(fresh_evidence["ok"], fresh_evidence["problems"])
        code, nothing, stderr = self.backfill(project, fresh, "run", carrier)
        self.assertEqual((code, nothing["state"], nothing.get("rows_this_run", 0)), (0, "complete", 0), stderr)
        self.timings["rows"] = ROWS
        self.timings["rows_inserted_during_backfill"] = len(inserted)

    # -- adversarial controls ----------------------------------------------- #

    def test_02_concurrent_index_conflicts_and_invalid_indexes(self) -> None:
        project = self.project_copy(PARCELS, "parcels_index")
        db = "parcels_index"
        url = self.cluster.create_database(db)
        self.tool(project, url, "alembic", "upgrade", "head")
        report = run_evolution(str(self.write_contract(EXPAND, project, "expand_index.json")), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        expand_rev = report["plan"]["migration"]["phases"][0]["revision"]
        self.tool(project, url, "alembic", "upgrade", expand_rev)
        # a different definition under the expected name is refused
        self.sql(db, "CREATE INDEX ix_shipments_carrier ON shipments (reference)")
        proc = self.tool(project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("different definition", proc.stderr + proc.stdout)
        self.sql(db, "DROP INDEX ix_shipments_carrier")
        # an identical index left invalid (an interrupted build) is dropped and rebuilt
        self.sql(db, "CREATE INDEX ix_shipments_carrier ON shipments (carrier)")
        self.sql(db, "UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'ix_shipments_carrier'::regclass")
        self.assertEqual(self.sql(db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_shipments_carrier'::regclass"), "f")
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("index_rebuild_upgrade_s", started)
        self.assertEqual(self.sql(db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_shipments_carrier'::regclass"), "t")
        # re-running is a no-op; downgrade drops it concurrently; upgrade recreates it
        self.tool(project, url, "alembic", "upgrade", "head")
        self.tool(project, url, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_class WHERE relname = 'ix_shipments_carrier'"), "0")
        self.tool(project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_shipments_carrier'::regclass"), "t")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM alembic_version"), "1")

    def test_03_connection_failure_and_time_limit_never_report_success(self) -> None:
        project = self.project_copy(PARCELS, "parcels_conn")
        url = self.cluster.create_database("parcels_conn")
        self.tool(project, url, "alembic", "upgrade", "head")
        run_evolution(str(self.write_contract(EXPAND, project, "expand_conn.json")), EvolveOptions(apply=True, verify=False))
        self.tool(project, url, "alembic", "upgrade", "head")
        carrier = next(c["op_id"] for c in read_manifest(str(project))["phased_changes"] if c["column"] == "carrier")
        bogus = url.rsplit("/", 1)[0] + "/does_not_exist"
        code, _, stderr = self.backfill(project, bogus, "run", carrier, check=False)
        self.assertEqual(code, 2, stderr)
        self.assertIn("database connection failed", stderr)
        self.sql("parcels_conn", "INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'r' || g FROM generate_series(1, 300) g")
        code, limited, _ = self.backfill(project, url, "run", carrier, "--batch-size", "50", "--max-seconds", "0", check=False)
        self.assertEqual((code, limited["state"], limited["batches_this_run"]), (0, "running", 0))
        code, resumed, _ = self.backfill(project, url, "run", carrier, "--batch-size", "50")
        self.assertEqual((resumed["state"], resumed["rows_processed"]), ("complete", 300))

    def test_04_row_level_security_tables_need_a_role_that_bypasses_rls(self) -> None:
        project = self.project_copy(TENANT_PROJECTS, "tenant_projects")
        db = "tenant_projects_phased"
        owner_url = self.cluster.create_database(db)
        self.cluster.create_role("fa_app_phased")
        doc = {"schema": "fastapi-architect/evolution/1.2", "project": {"path": str(project)}, "modify_modules": [{"name": "projects", "entities": [{"name": "Project", "add_fields": [{"name": "region", "type": "str", "max_length": 20, "required": True, "backfill": {"strategy": "constant", "value": "eu"}}]}]}]}
        contract = self.root / "tenant_expand.json"
        contract.write_text(json.dumps(doc), encoding="utf-8")
        report = run_evolution(str(contract), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assertTrue(any("row-level security" in r for r in report["plan"]["risks"]))
        region = next(c["op_id"] for c in read_manifest(str(project))["phased_changes"] if c["column"] == "region")
        self.tool(project, owner_url, "alembic", "upgrade", "head", TENANT_PROJECTS_APP_ROLE="fa_app_phased")
        self.sql(db, "INSERT INTO projects (id, tenant_id, name, budget, starts_on) SELECT gen_random_uuid(), 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'p' || g, 100, current_date FROM generate_series(1, 20) g")
        app_url = self.cluster.url_for(db, role="fa_app_phased")
        self.sql(db, "GRANT SELECT, INSERT, UPDATE ON tenant_projects_backfill_progress TO fa_app_phased")
        code, _, stderr = self.backfill(project, app_url, "run", region, check=False, TENANT_PROJECTS_MIGRATIONS_DATABASE_URL=app_url)
        self.assertEqual(code, 5, stderr)
        self.assertIn("row-level security", stderr)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM projects WHERE region IS NULL"), "20", "nothing was touched as the application role")
        code, done, _ = self.backfill(project, owner_url, "run", region)
        self.assertEqual((done["state"], done["rows_processed"]), ("complete", 20))


if __name__ == "__main__":
    unittest.main()
