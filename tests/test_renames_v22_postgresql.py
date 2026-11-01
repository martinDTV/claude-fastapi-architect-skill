"""Opt-in (``FA_DESIGN_PG=1``): renames in transition on a real, throwaway PostgreSQL (v2.2, increment 1).

The committed ``parcels`` snapshot is version A (it knows ``reference`` and ``weight_kg`` only). The rename
evolution produces version B (both names, old columns the source of truth). Both run as **separate processes from
their own virtual environments** against the same expanded database: an old-version driver and a transitional
driver, each a child process wrapping its application with ``TestClient`` and answering HTTP requests read from its
standard input. What is demonstrated: A keeps working on the expanded schema; B works before and after the backfill;
A's writes are mirrored by the sync trigger; B's writes land in both columns; B reads A's data and A reads B's;
concurrent updates of one row from both processes leave the pair consistent; a rolled-back write leaves both
columns untouched; the backfill (``mirror_column``) resumes after SIGKILL, waits for nobody on locked rows and
reconciles drift injected behind the trigger; the evidence reports ``columns_agree`` and the trigger state; the
plan-level and database-level adversarial cases refuse as documented; a rename on a row-level-security table
behaves under the application role. What is not demonstrated: a real distributed deployment with load balancers,
rolling restarts and clock skew; the two drivers are two processes on one machine sharing one database.
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
from typing import Dict, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS
from _pg_support import OWNER, ThrowawayCluster, find_pg, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
TENANT_PROJECTS = EXAMPLES / "generated" / "tenant-projects"
RENAME = EXAMPLES / "evolutions" / "parcels-rename-label.json"
ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVIDENCE_DIR = os.environ.get("FA_DESIGN_PG_EVIDENCE")
ROWS = 1500
SUITE = "uv run --group dev pytest -q -p no:cacheprovider"
API_KEY = "test-key"

DRIVER = '''
import json, os, sys
from fastapi.testclient import TestClient
from parcels.main import app
with TestClient(app) as client:
    for line in sys.stdin:
        request = json.loads(line)
        kwargs = {"headers": {"X-API-Key": os.environ["PARCELS_API_KEY"]}}
        if request["method"] in ("post", "patch", "put"):
            kwargs["json"] = request.get("json")
        response = getattr(client, request["method"])(request["path"], **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.text
        sys.stdout.write(json.dumps({"status": response.status_code, "body": body}) + "\\n")
        sys.stdout.flush()
'''


def _uv_env() -> Dict[str, str]:
    home = Path.home()
    return {"UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(home / ".cache" / "uv")), "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR", str(home / ".local" / "share" / "uv" / "python"))}


class AppDriver:
    """One application version as a child process of its own virtual environment, serving requests over pipes."""

    def __init__(self, project: Path, url: str, name: str) -> None:
        python = project / ".venv" / "bin" / "python"
        assert python.is_file(), f"{name}: the environment of {project} was not synced"
        env = dict(os.environ, PARCELS_DATABASE_URL=url, PARCELS_API_KEY=API_KEY, PARCELS_APP_NAME=name, PARCELS_ENVIRONMENT="local")
        self.name = name
        self.lock = threading.Lock()
        self.process = subprocess.Popen([str(python), "-c", DRIVER], cwd=str(project), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def request(self, method: str, path: str, body: Optional[dict] = None) -> tuple:
        with self.lock:
            assert self.process.stdin and self.process.stdout
            self.process.stdin.write(json.dumps({"method": method, "path": path, "json": body}) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            err = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"{self.name}: the driver died: {err[-2000:]}")
        answer = json.loads(line)
        return answer["status"], answer["body"]

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.close()  # type: ignore[union-attr]
                self.process.wait(timeout=20)
            except Exception:
                self.process.kill()


@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the rename transition against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class RenameTransitionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ThrowawayCluster().__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="fa-rename-pg-"))
        cls.timings: Dict[str, object] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / "rename-timings.json").write_text(json.dumps(cls.timings, indent=2, sort_keys=True), encoding="utf-8")

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

    def tool(self, project: Path, url: str, *args: str, check: bool = True, **extra: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(["uv", "run", "--group", "dev", *args], cwd=str(project), env=self.env(project, url, **extra), capture_output=True, text=True, timeout=600)
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

    def save(self, name: str, payload: object) -> None:
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def timed(self, label: str, started: float) -> None:
        self.timings[label] = round(time.time() - started, 3)

    def evolve(self, project: Path, contract_path: Path, verification_db: str) -> dict:
        for key, value in _uv_env().items():
            os.environ[key] = value
        os.environ["DATABASE_URL"] = verification_db
        options = EvolveOptions(apply=True, verify=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project.parent), declared_by="tests: v2.2 renames, FA_DESIGN_PG opt-in"), policy=ExecutionPolicy(authorized=True, command=SUITE, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + ("DATABASE_URL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")), test_command=SUITE)
        return run_evolution(str(contract_path), options)

    def rename_contract(self, project: Path, name: str) -> Path:
        doc = json.loads(RENAME.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def hold_lock(self, db: str, where: str) -> subprocess.Popen:
        psql = find_pg("psql")
        assert psql
        session = subprocess.Popen([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-q"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert session.stdin is not None
        session.stdin.write(f"BEGIN;\nSELECT id FROM shipments WHERE {where} FOR UPDATE;\n")
        session.stdin.flush()
        idle = f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{db}' AND state = 'idle in transaction'"
        deadline = time.time() + 30
        while time.time() < deadline and int(self.sql(db, idle)) < 1:
            time.sleep(0.05)
        self.assertGreaterEqual(int(self.sql(db, idle)), 1)
        self.addCleanup(lambda: session.poll() is None and session.kill())
        return session

    def release(self, session: subprocess.Popen) -> None:
        assert session.stdin is not None
        session.stdin.write("COMMIT;\n")
        session.stdin.flush()
        session.stdin.close()
        session.wait(timeout=30)

    # -- the coexistence of A and B ------------------------------------------- #

    def test_01_old_and_transitional_versions_coexist_on_the_expanded_schema(self) -> None:
        a_project = self.project_copy(PARCELS, "version_a")
        b_project = self.project_copy(PARCELS, "version_b")
        db = "coexist"
        url = self.cluster.create_database(db)
        started = time.time()
        self.tool(a_project, url, "alembic", "upgrade", "head")
        self.timed("a_baseline_upgrade_s", started)
        self.sql(db, f"INSERT INTO shipments (id, reference, weight_kg) SELECT gen_random_uuid(), 'ref-' || g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END FROM generate_series(1, {ROWS}) g")
        # B: the rename evolution, verified in a copy (its own suite on a separate database) and applied
        started = time.time()
        report = self.evolve(b_project, self.rename_contract(b_project, "rename.json"), self.cluster.create_database("coexist_verify"))
        self.timed("b_rename_evolution_verified_and_applied_s", started)
        self.save("rename-evolution.json", report)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons") or report["plan"]["blockers"])
        run = report["verification"]["tests"]["run"]
        self.assertEqual(run["counts"]["failed"], 0, run["counts"])
        passed = run["evidence"]["tests"]["passed"]
        for name in ("test_create_shipment_accepts_the_former_name_reference_and_returns_both_names", "test_update_shipment_through_the_former_name_reference_writes_both_columns", "test_the_sync_trigger_mirrors_writes_of_either_column", "test_backfill_fills_existing_rows_in_batches_and_is_idempotent", "test_verify_reports_the_state_of_this_database"):
            self.assertTrue(any(p.endswith(name) for p in passed), name)
        self.assertEqual(report["verification"]["audit"]["blocking_new"], [])
        ops = {c["column"]: c for c in read_manifest(str(b_project))["phased_changes"]}
        label_op, weight_op = ops["label"]["op_id"], ops["weight"]["op_id"]
        started = time.time()
        self.tool(b_project, url, "alembic", "upgrade", "head")
        self.timed("b_expand_upgrade_s", started)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' AND tgenabled = 'O'"), "2")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS NULL"), str(ROWS), "rows written before the trigger wait for the backfill")
        # both versions as separate processes from their own environments
        a = AppDriver(a_project, url, "version-a")
        b = AppDriver(b_project, url, "version-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        evidence: Dict[str, object] = {"rows": ROWS}
        # A works on the expanded schema: create, read, update with the old name only
        status, created_by_a = a.request("post", "/api/v1/shipments", {"reference": "from-a", "weight_kg": "1.50"})
        self.assertEqual(status, 201, created_by_a)
        self.assertEqual(self.sql(db, f"SELECT label || '|' || weight::text FROM shipments WHERE id = '{created_by_a['id']}'"), "from-a|1.50", "A's insert is mirrored into the new columns by the trigger")
        # B before the backfill: reads A's rows and the old rows through the old column
        status, seen_by_b = b.request("get", f"/api/v1/shipments/{created_by_a['id']}")
        self.assertEqual((status, seen_by_b["label"], seen_by_b["reference"], seen_by_b["weight"]), (200, "from-a", "from-a", "1.50"))
        old_row = self.sql(db, "SELECT id FROM shipments WHERE reference = 'ref-7'")
        status, old_seen = b.request("get", f"/api/v1/shipments/{old_row}")
        self.assertEqual((status, old_seen["label"], old_seen["reference"]), (200, "ref-7", "ref-7"), "B reads a row the backfill has not touched yet")
        # B writes both; A reads B's rows through the old name
        status, created_by_b = b.request("post", "/api/v1/shipments", {"label": "from-b"})
        self.assertEqual((status, created_by_b["label"], created_by_b["reference"]), (201, "from-b", "from-b"))
        status, created_old_name = b.request("post", "/api/v1/shipments", {"reference": "from-b-old-name"})
        self.assertEqual((status, created_old_name["label"]), (201, "from-b-old-name"), "B accepts the former name")
        status, a_sees_b = a.request("get", f"/api/v1/shipments/{created_by_b['id']}")
        self.assertEqual((status, a_sees_b["reference"]), (200, "from-b"))
        self.assertEqual(self.sql(db, f"SELECT reference IS NOT DISTINCT FROM label FROM shipments WHERE id = '{created_by_b['id']}'"), "t")
        # A updates B's row; B sees the new value (old column is the truth, the mirror follows)
        status, _ = a.request("patch", f"/api/v1/shipments/{created_by_b['id']}", {"reference": "a-updated"})
        self.assertEqual(status, 200)
        status, b_after = b.request("get", f"/api/v1/shipments/{created_by_b['id']}")
        self.assertEqual((b_after["label"], b_after["reference"]), ("a-updated", "a-updated"))
        # B updates A's row through either name; A sees it
        status, _ = b.request("patch", f"/api/v1/shipments/{created_by_a['id']}", {"label": "b-updated"})
        self.assertEqual(status, 200)
        status, a_after = a.request("get", f"/api/v1/shipments/{created_by_a['id']}")
        self.assertEqual((status, a_after["reference"]), (200, "b-updated"))
        status, _ = b.request("patch", f"/api/v1/shipments/{created_by_a['id']}", {"weight_kg": "2.25"})
        self.assertEqual(status, 200)
        self.assertEqual(self.sql(db, f"SELECT weight::text || '|' || weight_kg::text FROM shipments WHERE id = '{created_by_a['id']}'"), "2.25|2.25")
        # a NULL written by either version stays NULL in both
        status, _ = a.request("patch", f"/api/v1/shipments/{created_by_a['id']}", {"weight_kg": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.sql(db, f"SELECT weight IS NULL AND weight_kg IS NULL FROM shipments WHERE id = '{created_by_a['id']}'"), "t")
        # concurrent updates of one row from both processes: the pair stays consistent, the value is one of the writes
        target = created_by_b["id"]
        sent: Dict[str, list] = {"a": [], "b": []}

        def hammer(driver: AppDriver, key: str, field: str) -> None:
            for i in range(15):
                value = f"{key}-{i}"
                code, _body = driver.request("patch", f"/api/v1/shipments/{target}", {field: value})
                sent[key].append((code, value))

        threads = [threading.Thread(target=hammer, args=(a, "a", "reference")), threading.Thread(target=hammer, args=(b, "b", "label"))]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.timed("concurrent_updates_30_s", started)
        self.assertTrue(all(code == 200 for key in sent for code, _v in sent[key]), sent)
        final = self.sql(db, f"SELECT reference || '|' || label FROM shipments WHERE id = '{target}'")
        self.assertEqual(final.split("|")[0], final.split("|")[1], "both columns agree after interleaved updates from two versions")
        self.assertIn(final.split("|")[0], {v for key in sent for _c, v in sent[key]})
        # a rolled-back write leaves both columns untouched
        before = self.sql(db, f"SELECT reference || '|' || label FROM shipments WHERE id = '{target}'")
        self.sql(db, f"BEGIN; UPDATE shipments SET reference = 'rolled-back' WHERE id = '{target}'; ROLLBACK;")
        self.assertEqual(self.sql(db, f"SELECT reference || '|' || label FROM shipments WHERE id = '{target}'"), before)
        evidence.update(created_by_a=created_by_a["id"], created_by_b=created_by_b["id"], concurrent_final=final)
        # the backfill reconciles the pre-trigger rows while A keeps writing; SIGKILL and resume on the way
        venv_python = b_project / ".venv" / "bin" / "python"
        killed = subprocess.Popen([str(venv_python), "-m", "parcels.ops.backfill", "run", label_op, "--batch-size", "50", "--batch-pause-ms", "150"], cwd=str(b_project), env=self.env(b_project, url), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            done = self.sql(db, f"SELECT coalesce(rows_processed, 0) FROM parcels_backfill_progress WHERE op_id = '{label_op}'") or "0"
            if int(done) >= 300:
                break
            time.sleep(0.05)
        killed.send_signal(signal.SIGKILL)
        killed.communicate(timeout=60)
        processed = int(self.sql(db, f"SELECT rows_processed FROM parcels_backfill_progress WHERE op_id = '{label_op}'"))
        mirrored = int(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS NOT DISTINCT FROM reference"))
        self.assertGreaterEqual(mirrored, processed, "every processed row agrees (the trigger covers the others)")
        inserted_by_a: list = []

        def old_writer() -> None:
            for i in range(20):
                code, body = a.request("post", "/api/v1/shipments", {"reference": f"late-{i}"})
                inserted_by_a.append((code, body.get("id") if isinstance(body, dict) else None))
                time.sleep(0.01)

        writer = threading.Thread(target=old_writer)
        writer.start()
        started = time.time()
        for _attempt in range(20):
            code, resumed, stderr = self.backfill(b_project, url, "run", label_op, "--batch-size", "200", check=False)
            if code != 3:
                break
            time.sleep(0.5)
        writer.join(timeout=60)
        self.timed("label_backfill_resume_s", started)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(resumed["state"], "complete")
        self.assertTrue(all(c == 201 for c, _i in inserted_by_a), inserted_by_a)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference"), "0", "A's late inserts were mirrored by the trigger, the old rows by the backfill")
        code, weight_run, _ = self.backfill(b_project, url, "run", weight_op, "--batch-size", "500")
        self.assertEqual(weight_run["state"], "complete")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE weight IS DISTINCT FROM weight_kg"), "0", "NULL weights agree with NULL mirrors")
        # evidence: both operations agree, triggers enabled, the declared application version of B
        code, evidence_doc, _ = self.backfill(b_project, url, "verify", "--application-version", ops["label"]["application_version"], "--output", str(self.root / "rename-evidence.json"))
        self.assertTrue(evidence_doc["ok"], evidence_doc["problems"])
        self.assertEqual(evidence_doc["schema"], "fastapi-architect/migration-evidence/1.1")
        for op_id in (label_op, weight_op):
            entry = evidence_doc["operations"][op_id]
            self.assertEqual((entry["kind"], entry["columns_agree"], entry["mismatch_rows"], entry["sync"]["enabled"], entry["source_of_truth"]), ("rename", True, 0, True, "from_column"))
        self.save("rename-evidence.json", evidence_doc)
        # A still works after the backfill, and so does B
        status, _ = a.request("post", "/api/v1/shipments", {"reference": "after-backfill"})
        self.assertEqual(status, 201)
        status, listed = b.request("get", "/api/v1/shipments?limit=5")
        self.assertEqual(status, 200)
        self.assertTrue(all(item["label"] == item["reference"] for item in listed))
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference"), "0")
        self.save("rename-coexistence.json", evidence)
        self.timings["rows_inserted_by_a_during_backfill"] = len(inserted_by_a)

    # -- backfill controls: locked rows, drift behind the trigger, checkpoints -- #

    def test_02_locked_rows_drift_and_checkpoints(self) -> None:
        project = self.project_copy(PARCELS, "controls")
        db = "controls"
        url = self.cluster.create_database(db)
        self.tool(project, url, "alembic", "upgrade", "head")
        self.sql(db, "INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'c-' || g FROM generate_series(1, 400) g")
        run_evolution(str(self.rename_contract(project, "controls.json")), EvolveOptions(apply=True, verify=False))
        self.tool(project, url, "alembic", "upgrade", "head")
        ops = {c["column"]: c["op_id"] for c in read_manifest(str(project))["phased_changes"]}
        label_op = ops["label"]
        trigger = self.sql(db, "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' ORDER BY tgname LIMIT 1")
        first = self.sql(db, "SELECT id FROM shipments ORDER BY id LIMIT 1")
        lock = self.hold_lock(db, f"id = '{first}'")
        code, report, stderr = self.backfill(project, url, "run", label_op, "--batch-size", "100", check=False)
        self.assertEqual(code, 6, stderr)
        self.assertEqual((report["state"], report["rows_this_run"], report["pending_rows"]), ("incomplete", 399, 1))
        self.release(lock)
        code, report, _ = self.backfill(project, url, "run", label_op, "--batch-size", "100")
        self.assertEqual((report["state"], report["rows_this_run"]), ("complete", 1))
        # drift injected behind the trigger (a maintenance script that disabled it): verify names it, a run fixes it
        drift = self.sql(db, "SELECT id FROM shipments ORDER BY id OFFSET 10 LIMIT 1")
        self.sql(db, f"ALTER TABLE shipments DISABLE TRIGGER {trigger}; UPDATE shipments SET reference = 'drifted' WHERE id = '{drift}'; ALTER TABLE shipments ENABLE TRIGGER {trigger}")
        code, evidence, _ = self.backfill(project, url, "verify", check=False)
        self.assertFalse(evidence["ok"])
        self.assertEqual((evidence["operations"][label_op]["mismatch_rows"], evidence["operations"][label_op]["columns_agree"]), (1, False))
        code, reopened, _ = self.backfill(project, url, "run", label_op)
        self.assertEqual((reopened["state"], reopened["rows_this_run"]), ("complete", 1))
        self.assertIn("reopened", reopened["note"])
        self.assertEqual(self.sql(db, f"SELECT label FROM shipments WHERE id = '{drift}'"), "drifted", "the old column is the truth: the mirror takes its value")
        # a disabled trigger is a problem the evidence reports even when the columns agree
        self.sql(db, f"ALTER TABLE shipments DISABLE TRIGGER {trigger}")
        code, evidence, _ = self.backfill(project, url, "verify", check=False)
        self.assertFalse(evidence["ok"])
        self.assertTrue(any("disabled" in p for p in evidence["problems"]), evidence["problems"])
        self.sql(db, f"ALTER TABLE shipments ENABLE TRIGGER {trigger}")
        # checkpoints that cannot be trusted keep their v2.1 semantics
        self.sql(db, f"UPDATE parcels_backfill_progress SET state = 'running', last_key = 'garbage' WHERE op_id = '{label_op}'")
        code, _, stderr = self.backfill(project, url, "run", label_op, check=False)
        self.assertEqual(code, 4, stderr)
        self.sql(db, f"UPDATE parcels_backfill_progress SET last_key = NULL, checksum = 'stale' WHERE op_id = '{label_op}'")
        code, _, stderr = self.backfill(project, url, "run", label_op, check=False)
        self.assertEqual(code, 4, stderr)
        # the expand revision is reversible without data loss: the old columns hold everything
        self.tool(project, url, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync'"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('label', 'weight')"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IS NOT NULL"), "400")
        self.tool(project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync'"), "2")

    # -- adversarial cases at the plan and the database level -------------------- #

    def test_03_adversarial_plans_and_schemas(self) -> None:
        project = self.project_copy(PARCELS, "adversarial")
        db = "adversarial"
        url = self.cluster.create_database(db)
        self.tool(project, url, "alembic", "upgrade", "head")
        # a column of the new name already present by hand (another type): the expand revision fails and the database is unchanged
        self.sql(db, "ALTER TABLE shipments ADD COLUMN label integer")
        run_evolution(str(self.rename_contract(project, "adversarial.json")), EvolveOptions(apply=True, verify=False))
        proc = self.tool(project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already exists", proc.stderr + proc.stdout)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM alembic_version WHERE version_num = (SELECT version_num FROM alembic_version)"), "1")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync'"), "0", "the transactional revision left nothing behind")
        self.assertEqual(self.sql(db, "SELECT data_type FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'label'"), "integer")
        self.sql(db, "ALTER TABLE shipments DROP COLUMN label")
        self.tool(project, url, "alembic", "upgrade", "head")
        # plan-level refusals on the evolved project
        manifest = read_manifest(str(project))
        stale = json.loads(RENAME.read_text(encoding="utf-8"))
        stale["project"] = {"path": str(project), "expected_spec_digest": "000000000000"}
        stale["modify_modules"][0]["entities"][0]["rename_fields"] = [{"from": "label", "to": "tag"}]
        plan = plan_evolution(load_evolution(str(self._write("stale.json", stale))))
        self.assertTrue(any("stale" in b or "expected_spec_digest" in b for b in plan.blockers), plan.blockers)
        manual = self.project_copy(project, "manual")
        readme = manual / "README.md"  # an integration point every evolution regenerates
        readme.write_text(readme.read_text(encoding="utf-8") + "\nOperator note.\n", encoding="utf-8")
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(manual)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "add_fields": [{"name": "note", "type": "str", "required": False}]}]}]}
        plan = plan_evolution(load_evolution(str(self._write("manual.json", doc))))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("manual_change_on_integration_point") for b in plan.blockers), plan.blockers)
        altered = self.project_copy(project, "altered")
        initial = next(altered.glob("migrations/versions/*_initial_schema.py"))
        initial.write_text(initial.read_text(encoding="utf-8") + "\n# altered by hand\n", encoding="utf-8")
        plan = plan_evolution(load_evolution(str(self._write("altered.json", dict(doc, project={"path": str(altered)})))))
        self.assertTrue(any(b.startswith("historical_migrations_unchanged") for b in plan.blockers), plan.blockers)
        heads = self.project_copy(project, "heads")
        expand = next(heads.glob("migrations/versions/*_expand_*.py"))
        (heads / "migrations" / "versions" / "ffffffffffff_second_head.py").write_text(expand.read_text(encoding="utf-8").replace(f'revision = "{manifest["migrations"][-1]["revision"]}"', 'revision = "ffffffffffff"'), encoding="utf-8")
        plan = plan_evolution(load_evolution(str(self._write("heads.json", dict(doc, project={"path": str(heads)})))))
        self.assertTrue(any(b.startswith("alembic_chain_is_linear") or b.startswith("alembic_head_matches_manifest") for b in plan.blockers), plan.blockers)
        premature = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "contract_fields": [{"name": "label", "operation": manifest["phased_changes"][0]["op_id"]}]}]}], "authorization": {"contract": {"declared_by": "someone", "evidence": "none.json"}}}
        plan = plan_evolution(load_evolution(str(self._write("premature.json", premature))), authorize_contract=True)
        self.assertTrue(any(b.startswith("contract_rename_available") for b in plan.blockers), plan.blockers)

    def _write(self, name: str, doc: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    # -- row-level security ----------------------------------------------------- #

    def test_04_a_rename_on_a_row_level_security_table_mirrors_the_application_roles_writes(self) -> None:
        project = self.project_copy(TENANT_PROJECTS, "rls")
        db = "rls"
        owner_url = self.cluster.create_database(db)
        self.cluster.create_role("fa_app_rename")
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "projects", "entities": [{"name": "Project", "rename_fields": [{"from": "name", "to": "title"}]}]}]}
        report = run_evolution(str(self._write("rls.json", doc)), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.tool(project, owner_url, "alembic", "upgrade", "head", TENANT_PROJECTS_APP_ROLE="fa_app_rename")
        tenant = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        app_url = self.cluster.url_for(db, role="fa_app_rename")
        # the application role writes the old name under its tenant context: the trigger mirrors it under the policy
        psql = find_pg("psql")
        assert psql
        insert = f"BEGIN; SELECT set_config('app.tenant_id', '{tenant}', true); INSERT INTO projects (id, tenant_id, name, budget, starts_on) VALUES (gen_random_uuid(), '{tenant}', 'as-app', 10, current_date) RETURNING id; COMMIT;"
        proc = subprocess.run([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", "fa_app_rename", "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-c", insert], capture_output=True, text=True, env=self.cluster._env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sql(db, "SELECT title || '|' || name FROM projects WHERE name = 'as-app'"), "as-app|as-app")
        # the backfill refuses the application role and runs as the owner
        op_id = read_manifest(str(project))["phased_changes"][0]["op_id"]
        code, _, stderr = self.backfill(project, app_url, "run", op_id, check=False, TENANT_PROJECTS_MIGRATIONS_DATABASE_URL=app_url)
        self.assertEqual(code, 5, stderr)
        code, done, _ = self.backfill(project, owner_url, "run", op_id)
        self.assertEqual(done["state"], "complete")
        tenant_plan = plan_evolution(load_evolution(str(self._write("rls-tenant.json", {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "projects", "entities": [{"name": "Project", "rename_fields": [{"from": "tenant_id", "to": "org_id"}]}]}]}))))
        self.assertTrue(any(b.startswith("rename_protected_column") or b.startswith("rename_source_exists") for b in tenant_plan.blockers), tenant_plan.blockers)


if __name__ == "__main__":
    unittest.main()
