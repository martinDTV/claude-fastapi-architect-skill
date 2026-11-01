"""Opt-in (``FA_DESIGN_PG=1``): the retire and the destructive contract of a rename on a real, throwaway PostgreSQL
(v2.2, increment 2), on top of the A/B stage of ``test_renames_v22_postgresql.py``.

Three generations run as **separate processes from their own virtual environments** against one database: A (the
committed snapshot: ``reference`` and ``weight_kg`` only), B (the rename evolution: both names, old columns the
truth) and C (the retire evolution: ``label`` and ``weight`` only, the new columns the truth). What is demonstrated:
B and C coexist on the retire schema (C writes the new column and the trigger fills the old one; B writes both; each
reads what the other wrote; partial updates and NULLs stay consistent; interleaved updates leave the pair equal); B
refuses a request that sends both names (422, equal or not: one name per request) and the trigger refuses one
statement that sets both to different values (SQLSTATE 23514); A keeps working through the trigger but is identified in the evidence as an
unidentified session; the retire revision is reversible (downgrade, upgrade); the destructive contract is refused
while A or B are connected, while a view, an index or a lock-holding session stands in the way, while the trigger is
disabled or the columns differ, and succeeds, in one transaction and with RESTRICT, once every condition holds; C
keeps working after the drop; B fails (incompatible, as documented); the downgrade recreates the column from the new
one's current values and B works again. Not demonstrated: a distributed deployment (load balancers, rolling
restarts, clock skew); the processes share one machine and one database; instances that connect intermittently are
invisible to a snapshot of ``pg_stat_activity``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, Optional, Tuple

from test_renames_v22_postgresql import API_KEY, PARCELS, ROWS, SUITE, PostgresRenameCase, _uv_env

from _audit_support import REPO_ROOT, SKILL_SCRIPTS
from _pg_support import OWNER, find_pg, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_design.blueprint.phased import confirmation_phrase  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVOLVE_CLI = SKILL_SCRIPTS / "evolve_fastapi.py"
A_V210 = REPO_ROOT / "tests" / "fixtures" / "compat" / "generation-1.3" / "parcels"  # the parcels project as v2.1.0 published it: no application tag
INVARIANTS = ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"]

DRIVER = '''
import json, os, sys
from fastapi.testclient import TestClient
from parcels.main import app
with TestClient(app, raise_server_exceptions=False) as client:
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


class Driver:
    """One application generation as a child process of its own virtual environment; server errors come back as 500."""

    def __init__(self, project: Path, url: str, name: str) -> None:
        python = project / ".venv" / "bin" / "python"
        assert python.is_file(), f"{name}: the environment of {project} was not synced"
        env = dict(os.environ, PARCELS_DATABASE_URL=url, PARCELS_API_KEY=API_KEY, PARCELS_APP_NAME=name, PARCELS_ENVIRONMENT="local")
        self.name = name
        self.lock = threading.Lock()
        self.process = subprocess.Popen([str(python), "-c", DRIVER], cwd=str(project), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, object]:
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


class RetireContractCase(PostgresRenameCase):
    """The helpers of the retire and contract stages (the acceptance module of increment 2 builds on them)."""

    TIMINGS = "retire-timings.json"

    # -- helpers ------------------------------------------------------------- #

    def evolve_with(self, project: Path, contract_path: Path, verification_db: Optional[str], *, verify: bool = True, authorize_contract: bool = False, confirm_irreversible: Optional[str] = None) -> dict:
        for key, value in _uv_env().items():
            os.environ[key] = value
        if verification_db:
            os.environ["DATABASE_URL"] = verification_db
        if not verify:  # apply after the structural verification only (no test run): the stage is exercised by this test itself
            return run_evolution(str(contract_path), EvolveOptions(apply=True, verify=False, authorize_contract=authorize_contract, confirm_irreversible=confirm_irreversible))
        policy = ExecutionPolicy(authorized=True, command=SUITE, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + ("DATABASE_URL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"))
        authorization = Authorization(execution=True, trust="fixture", scope=str(project.parent), declared_by="tests: v2.2 retire and contract, FA_DESIGN_PG opt-in")
        options = EvolveOptions(apply=True, verify=True, authorization=authorization, policy=policy, test_command=SUITE, authorize_contract=authorize_contract, confirm_irreversible=confirm_irreversible)
        return run_evolution(str(contract_path), options)

    def sql_attempt(self, db: str, statement: str) -> Tuple[int, str]:
        psql = find_pg("psql")
        assert psql
        proc = subprocess.run([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-c", statement], capture_output=True, text=True, env=self.cluster._env, timeout=120)
        return proc.returncode, proc.stderr.strip()

    def write(self, name: str, doc: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def stage_b(self, label: str, rows: int = ROWS, a_source: Path = PARCELS) -> Tuple[Path, Path, str, str]:
        """A on a fresh database with rows, then B (the rename evolution) applied: expanded schema, backfills done."""
        a_project = self.project_copy(a_source, f"{label}-a")
        b_project = self.project_copy(PARCELS, f"{label}-b")
        url = self.cluster.create_database(label)
        self.tool(a_project, url, "alembic", "upgrade", "head")
        self.sql(label, f"INSERT INTO shipments (id, reference, weight_kg) SELECT gen_random_uuid(), 'ref-' || g, CASE WHEN g % 3 = 0 THEN NULL ELSE g END FROM generate_series(1, {rows}) g")
        report = run_evolution(str(self.rename_contract(b_project, f"{label}-rename.json")), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.tool(b_project, url, "alembic", "upgrade", "head")
        for op in read_manifest(str(b_project))["phased_changes"]:
            code, done, stderr = self.backfill(b_project, url, "run", op["op_id"], "--batch-size", "500")
            self.assertEqual(done["state"], "complete", stderr)
        return a_project, b_project, label, url

    def retire_contract(self, project: Path, evidence: Path, name: str) -> Path:
        ops = {c["column"]: c for c in read_manifest(str(project))["phased_changes"]}
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "authorization": {"contract": {"declared_by": "tests: release owner", "evidence": str(evidence)}}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "retire_fields": [{"name": "label", "operation": ops["label"]["op_id"]}, {"name": "weight", "operation": ops["weight"]["op_id"]}]}]}], "invariants": INVARIANTS}
        return self.write(name, doc)

    def drop_contract(self, project: Path, evidence: Path, name: str, **destructive: object) -> Path:
        ops = {c["column"]: c for c in read_manifest(str(project))["phased_changes"]}
        block = {"approved_by": "tests: approver", "old_versions_stopped": True, "min_retire_age_hours": 0, **destructive}
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "authorization": {"contract": {"declared_by": "tests: release owner", "evidence": str(evidence)}, "destructive": block}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "contract_fields": [{"name": "label", "operation": ops["label"]["op_id"]}, {"name": "weight", "operation": ops["weight"]["op_id"]}]}]}], "invariants": [i for i in INVARIANTS if i != "no_destructive_schema_change"]}
        return self.write(name, doc)

    def stage_c(self, b_project: Path, url: str, db: str, label: str, *, verify: bool) -> Tuple[Path, dict]:
        """C: the retire evolution on a copy of B, from B's own evidence; the retire revision applied on the shared database."""
        b_version = {c["column"]: c for c in read_manifest(str(b_project))["phased_changes"]}["label"]["application_version"]
        code, evidence, stderr = self.backfill(b_project, url, "verify", "--application-version", b_version, "--output", str(self.root / f"{label}-evidence-b.json"))
        self.assertTrue(evidence["ok"], evidence["problems"])
        c_project = self.project_copy(b_project, f"{label}-c")
        started = time.time()
        report = self.evolve_with(c_project, self.retire_contract(c_project, self.root / f"{label}-evidence-b.json", f"{label}-retire.json"), self.cluster.create_database(f"{label}_verify_c") if verify else None, verify=verify)
        self.timed(f"{label}_retire_evolution{'_verified' if verify else ''}_s", started)
        self.save(f"{label}-retire-evolution.json", report)
        expected = "verified" if verify else "applied"
        state = report["state"] if verify else report["applied"]["state"]
        self.assertEqual(state, expected, report.get("verification", {}).get("reasons") if verify else report["plan"]["blockers"])
        self.assertEqual(report["applied"]["state"], "applied")
        started = time.time()
        self.tool(c_project, url, "alembic", "upgrade", "head")
        self.timed(f"{label}_retire_upgrade_s", started)
        return c_project, report



@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the retire and contract stages against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class RetireContractPostgresTests(RetireContractCase):
    # -- B and C on the retire schema -------------------------------------------- #

    def test_01_transitional_and_retired_versions_coexist_on_the_retire_schema(self) -> None:
        a_project, b_project, db, url = self.stage_b("retire", a_source=A_V210)  # A exactly as v2.1.0 generated it
        c_project, report = self.stage_c(b_project, url, db, "retire", verify=True)
        self.save("retire-evolution.json", report)
        run = report["verification"]["tests"]["run"]
        self.assertEqual(run["counts"]["failed"], 0, run["counts"])
        passed = run["evidence"]["tests"]["passed"]
        for name in ("test_the_sync_trigger_mirrors_writes_of_either_column", "test_the_sync_trigger_refuses_one_statement_that_sets_both_names_to_different_values", "test_migrated_schema_matches_the_models"):
            self.assertTrue(any(p.endswith(name) for p in passed), name)
        self.assertFalse(any("reference" in p for p in passed), "C's suite knows the new name only")
        # the retire revision on the shared database: NOT NULL carried, the comment stamped, the old columns kept
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'label'"), "NO")
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'weight'"), "YES")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "2")
        comments = self.sql(db, "SELECT string_agg(obj_description(p.oid, 'pg_proc'), '|' ORDER BY p.proname) FROM pg_proc p WHERE p.proname LIKE 'fn_shipments_%_sync'")
        self.assertIn("truth=label", comments)
        self.assertIn("truth=weight ", comments + " ")
        self.assertIn("retired_at=", comments)
        a = Driver(a_project, url, "version-a")
        b = Driver(b_project, url, "version-b")
        c = Driver(c_project, url, "version-c")
        for driver in (a, b, c):
            self.addCleanup(driver.stop)
        # C writes the new name only; the trigger fills the old column; B reads it under both names, A under the old
        status, by_c = c.request("post", "/api/v1/shipments", {"label": "from-c", "weight": "2.50"})
        self.assertEqual(status, 201, by_c)
        self.assertNotIn("reference", by_c)
        self.assertEqual(self.sql(db, f"SELECT reference || '|' || weight_kg::text FROM shipments WHERE id = '{by_c['id']}'"), "from-c|2.50")
        status, b_sees = b.request("get", f"/api/v1/shipments/{by_c['id']}")
        self.assertEqual((status, b_sees["label"], b_sees["reference"], b_sees["weight_kg"]), (200, "from-c", "from-c", "2.50"))
        status, a_sees = a.request("get", f"/api/v1/shipments/{by_c['id']}")
        self.assertEqual((status, a_sees["reference"]), (200, "from-c"))
        # B writes both names; C reads the new one
        status, by_b = b.request("post", "/api/v1/shipments", {"reference": "from-b"})
        self.assertEqual(status, 201, by_b)
        status, c_sees = c.request("get", f"/api/v1/shipments/{by_b['id']}")
        self.assertEqual((status, c_sees["label"]), (200, "from-b"))
        self.assertNotIn("reference", c_sees)
        # A still writes the old column: tolerated, mirrored (nothing lost), but not part of the retire contract
        status, by_a = a.request("post", "/api/v1/shipments", {"reference": "from-a"})
        self.assertEqual(status, 201, by_a)
        status, c_sees_a = c.request("get", f"/api/v1/shipments/{by_a['id']}")
        self.assertEqual((status, c_sees_a["label"]), (200, "from-a"))
        # partial updates through either generation keep the pair equal; NULL propagates
        status, _ = c.request("patch", f"/api/v1/shipments/{by_b['id']}", {"weight": "7.25"})
        self.assertEqual(status, 200)
        self.assertEqual(self.sql(db, f"SELECT weight_kg::text || '|' || reference FROM shipments WHERE id = '{by_b['id']}'"), "7.25|from-b")
        status, _ = b.request("patch", f"/api/v1/shipments/{by_c['id']}", {"weight_kg": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.sql(db, f"SELECT weight IS NULL AND weight_kg IS NULL FROM shipments WHERE id = '{by_c['id']}'"), "t")
        status, _ = b.request("patch", f"/api/v1/shipments/{by_c['id']}", {"reference": "b-renamed"})
        self.assertEqual(status, 200)
        status, c_after = c.request("get", f"/api/v1/shipments/{by_c['id']}")
        self.assertEqual(c_after["label"], "b-renamed")
        # B refuses both names in one request at its boundary (equal or not); one name passes
        status, body = b.request("post", "/api/v1/shipments", {"label": "one", "reference": "two"})
        self.assertEqual(status, 422, body)
        status, body = b.request("patch", f"/api/v1/shipments/{by_c['id']}", {"label": "one", "reference": "two"})
        self.assertEqual(status, 422, body)
        status, body = b.request("post", "/api/v1/shipments", {"label": "same", "reference": "same"})
        self.assertEqual(status, 422, "both names in one request are ambiguous even when equal: one name per request")
        status, body = b.request("post", "/api/v1/shipments", {"reference": "one-name"})
        self.assertEqual((status, body["label"], body["reference"]), (201, "one-name", "one-name"))
        # the trigger refuses one statement that sets both names to different values; the row is untouched
        code, stderr = self.sql_attempt(db, f"UPDATE shipments SET reference = 'x', label = 'y' WHERE id = '{by_b['id']}'")
        self.assertNotEqual(code, 0)
        self.assertIn("set to different values in one statement", stderr)
        self.assertEqual(self.sql(db, f"SELECT reference || '|' || label FROM shipments WHERE id = '{by_b['id']}'"), "from-b|from-b")
        code, stderr = self.sql_attempt(db, "INSERT INTO shipments (id, reference, label) VALUES (gen_random_uuid(), 'x', 'y')")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IN ('x', 'y')"), "0")
        # the new column is NOT NULL now: a NULL through the old name is refused like before the rename
        code, _ = self.sql_attempt(db, f"UPDATE shipments SET reference = NULL WHERE id = '{by_b['id']}'")
        self.assertNotEqual(code, 0)
        # interleaved updates from B and C on one row: the pair stays equal, the value is one of the writes
        target = by_b["id"]
        sent: Dict[str, list] = {"b": [], "c": []}

        def hammer(driver: Driver, key: str, field: str) -> None:
            for i in range(15):
                value = f"{key}-{i}"
                code, _body = driver.request("patch", f"/api/v1/shipments/{target}", {field: value})
                sent[key].append((code, value))

        threads = [threading.Thread(target=hammer, args=(b, "b", "reference")), threading.Thread(target=hammer, args=(c, "c", "label"))]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.timed("retire_concurrent_updates_30_s", started)
        self.assertTrue(all(code == 200 for key in sent for code, _v in sent[key]), sent)
        final = self.sql(db, f"SELECT reference || '|' || label FROM shipments WHERE id = '{target}'")
        self.assertEqual(final.split("|")[0], final.split("|")[1])
        self.assertIn(final.split("|")[0], {v for key in sent for _c, v in sent[key]})
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference OR weight IS DISTINCT FROM weight_kg"), "0")
        # the evidence written by C's runner: truth, retired_at, the sessions of three generations
        c_version = {x["column"]: x for x in read_manifest(str(c_project))["phased_changes"]}["label"]["application_version"]
        code, evidence, _ = self.backfill(c_project, url, "verify", "--application-version", c_version, "--output", str(self.root / "retire-evidence-c.json"))
        self.assertTrue(evidence["ok"], evidence["problems"])
        self.assertEqual(evidence["schema"], "fastapi-architect/migration-evidence/1.2")
        names = {s["application_name"] for s in evidence["sessions"]}
        tags = {read_manifest(str(p))["application_tag"] for p in (b_project, c_project)}
        self.assertTrue(tags <= names, (tags, names))
        self.assertIn("", names, "A as v2.1.0 generated it announces no application name: an unidentified session")
        self.assertEqual(len(tags), 2, "B and C carry different tags")
        for entry in evidence["operations"].values():
            self.assertEqual((entry["state_declared"], entry["sync"]["truth"], entry["columns_agree"], entry["unmanaged_dependents"]), ("retire", entry["column"], True, []))
            self.assertIsNotNone(entry["sync"]["retired_at"])
        self.save("retire-evidence.json", evidence)
        # the retire revision is reversible: downgrade puts the old column back in charge, upgrade moves on again
        for driver in (a, b, c):
            driver.stop()
        self.tool(c_project, url, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'label'"), "YES")
        self.assertIn("truth=reference", self.sql(db, "SELECT string_agg(obj_description(p.oid, 'pg_proc'), '|') FROM pg_proc p WHERE p.proname LIKE 'fn_shipments_%_sync'"))
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference"), "0")
        self.tool(c_project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'label'"), "NO")

    # -- the destructive contract --------------------------------------------- #

    def test_02_the_destructive_contract_drops_the_old_column_only_when_everything_holds(self) -> None:
        a_project, b_project, db, url = self.stage_b("drop", rows=600)
        c_project, _report = self.stage_c(b_project, url, db, "drop", verify=False)
        c_version = {x["column"]: x for x in read_manifest(str(c_project))["phased_changes"]}["label"]["application_version"]
        c_tag = read_manifest(str(c_project))["application_tag"]
        phrase = confirmation_phrase(read_manifest(str(c_project))["phased_changes"])
        a = Driver(a_project, url, "version-a")
        b = Driver(b_project, url, "version-b")
        c = Driver(c_project, url, "version-c")
        for driver in (a, b, c):
            self.addCleanup(driver.stop)
        for driver in (a, b, c):
            self.assertEqual(driver.request("get", "/api/v1/shipments?limit=1")[0], 200)
        a_tag = read_manifest(str(a_project))["application_tag"]  # A regenerated by this version carries the baseline tag: identified, not compatible
        evidence_path = self.root / "drop-evidence.json"

        def verify() -> dict:
            code, doc, stderr = self.backfill(c_project, url, "verify", "--application-version", c_version, "--output", str(evidence_path))
            self.assertTrue(doc["ok"], doc["problems"])
            return doc

        def blockers(**kw) -> list:
            plan = plan_evolution(load_evolution(str(self.drop_contract(c_project, evidence_path, "drop-contract.json"))), **kw)
            return sorted({x.split(":")[0] for x in plan.blockers})

        # A and B connected: identified, refused; the flags alone change nothing
        evidence = verify()
        self.save("drop-evidence-with-older-versions.json", evidence)
        self.assertIn(a_tag, {s["application_name"] for s in evidence["sessions"]})
        self.assertEqual(blockers(), ["contract_authorized", "contract_deployments_identified", "contract_irreversible_confirmed"])
        self.assertEqual(blockers(authorize_contract=True, confirm_irreversible=phrase), ["contract_deployments_identified"])
        b.stop()
        time.sleep(0.5)
        verify()
        self.assertEqual(blockers(authorize_contract=True, confirm_irreversible=phrase), ["contract_deployments_identified"], "A, unidentified, still blocks")
        a.stop()
        time.sleep(0.5)
        evidence = verify()
        self.assertEqual({s["application_name"] for s in evidence["sessions"]} - {"parcels.ops.backfill"}, {c_tag})
        self.assertEqual(blockers(authorize_contract=True, confirm_irreversible=phrase), [])
        self.assertEqual(blockers(authorize_contract=True), ["contract_irreversible_confirmed"])
        # the command line: the plan names the phrase it needs
        proc = subprocess.run([sys.executable, str(EVOLVE_CLI), "plan", str(self.root / "drop-contract.json"), "--no-verify", "--authorize-contract", "--format", "json"], capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT))
        cli = json.loads(proc.stdout)
        self.assertEqual(cli["state"], "blocked")
        self.assertTrue(any(x.startswith("contract_irreversible_confirmed") and phrase in x for x in cli["plan"]["blockers"]), cli["plan"]["blockers"])
        # the destructive evolution: D is generated and verified on its own database (its migrations run through the drop)
        d_project = self.project_copy(c_project, "drop-d")
        started = time.time()
        report = self.evolve_with(d_project, self.drop_contract(d_project, evidence_path, "drop-contract-d.json", ignore_sessions=["psql"]), self.cluster.create_database("drop_verify_d"), verify=True, authorize_contract=True, confirm_irreversible=phrase)  # the lock holder below is a psql console, named on purpose
        self.timed("drop_evolution_verified_s", started)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons") or report["plan"]["blockers"])
        self.assertEqual(report["applied"]["state"], "applied")
        self.save("drop-evolution.json", report)
        drop_file = next(d_project.glob("migrations/versions/*_drop_*.py"))
        self.assertIn("RESTRICT", drop_file.read_text(encoding="utf-8"))
        # adversarial, on the live database: each one makes `alembic upgrade head` fail and leaves the schema intact
        head_before = self.sql(db, "SELECT version_num FROM alembic_version")

        def upgrade_fails(reason: str) -> None:
            proc = self.tool(d_project, url, "alembic", "upgrade", "head", check=False)
            self.assertNotEqual(proc.returncode, 0, reason)
            self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head_before, reason)
            self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "2", reason)
            self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' AND tgenabled = 'O'"), "2", reason)
            return proc

        self.sql(db, "CREATE VIEW shipments_report AS SELECT id, reference FROM shipments")
        proc = upgrade_fails("a manual view depends on the old column")
        self.assertIn("dependents the tool does not manage", proc.stderr + proc.stdout)
        self.sql(db, "DROP VIEW shipments_report")
        self.sql(db, "CREATE INDEX ix_manual_reference ON shipments (reference)")
        proc = upgrade_fails("a manual index on the old column")
        self.assertIn("index ix_manual_reference", proc.stderr + proc.stdout)
        self.sql(db, "DROP INDEX ix_manual_reference")
        self.sql(db, "CREATE FUNCTION audit_reference() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reference IS NULL THEN RAISE EXCEPTION 'no'; END IF; RETURN NEW; END $$")
        self.sql(db, "CREATE TRIGGER zz_audit BEFORE UPDATE ON shipments FOR EACH ROW EXECUTE FUNCTION audit_reference()")
        proc = upgrade_fails("an unknown trigger function names the old column")
        self.assertIn("audit_reference", proc.stderr + proc.stdout)
        self.sql(db, "DROP TRIGGER zz_audit ON shipments; DROP FUNCTION audit_reference()")
        trigger = self.sql(db, "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' ORDER BY tgname LIMIT 1")
        self.sql(db, f"ALTER TABLE shipments DISABLE TRIGGER {trigger}")
        proc = self.tool(d_project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0, "a disabled sync trigger")
        self.assertIn("disabled", proc.stderr + proc.stdout)
        self.sql(db, f"UPDATE shipments SET reference = 'drifted' WHERE id = (SELECT id FROM shipments ORDER BY id LIMIT 1); ALTER TABLE shipments ENABLE TRIGGER {trigger}")
        proc = upgrade_fails("a row where the columns differ")
        self.assertIn("row(s) where label differs from reference", proc.stderr + proc.stdout)
        code, fixed, _ = self.backfill(c_project, url, "run", {x["column"]: x for x in read_manifest(str(c_project))["phased_changes"]}["label"]["op_id"])
        self.assertEqual(fixed["state"], "complete")
        holder = self.hold_lock(db, "true")
        started = time.time()
        proc = upgrade_fails("a session holding row locks: lock_timeout, not an indefinite wait")
        self.timed("drop_lock_timeout_s", started)
        self.assertIn("lock timeout", (proc.stderr + proc.stdout).lower())
        self.release(holder)
        # rows written after the evidence, by the retired generation, are fine: the trigger keeps the pair equal
        for i in range(5):
            self.assertEqual(c.request("post", "/api/v1/shipments", {"label": f"late-{i}"})[0], 201)
        started = time.time()
        self.tool(d_project, url, "alembic", "upgrade", "head")
        self.timed("drop_upgrade_s", started)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync'"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'fn_shipments_%_sync'"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label LIKE 'late-%'"), "5")
        # C keeps working after the drop; B is incompatible and says so
        status, after = c.request("post", "/api/v1/shipments", {"label": "after-drop", "weight": "1.00"})
        self.assertEqual(status, 201, after)
        self.assertEqual(c.request("get", "/api/v1/shipments?limit=3")[0], 200)
        b = Driver(b_project, url, "version-b-again")
        self.addCleanup(b.stop)
        status, body = b.request("get", "/api/v1/shipments?limit=1")
        self.assertEqual(status, 500, "B still maps the dropped column: incompatible with the contract schema")
        b.stop()
        # the evidence of the contracted state
        code, evidence, _ = self.backfill(d_project, url, "verify", "--application-version", c_version)
        self.assertTrue(evidence["ok"], evidence["problems"])
        for entry in evidence["operations"].values():
            self.assertEqual((entry["state_declared"], entry["source_present"], entry["sync"]["present"]), ("contract", False, False))
        self.save("drop-evidence-after.json", evidence)
        # conditional reversibility: the downgrade recreates the column from the new one's current values; B works again
        before = self.sql(db, "SELECT count(*) FROM shipments")
        started = time.time()
        self.tool(d_project, url, "alembic", "downgrade", "-1")
        self.timed("drop_downgrade_s", started)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IS DISTINCT FROM label OR weight_kg IS DISTINCT FROM weight"), "0")
        self.assertEqual(self.sql(db, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'reference'"), "NO")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' AND tgenabled = 'O'"), "2")
        self.assertIn("truth=label", self.sql(db, "SELECT string_agg(obj_description(p.oid, 'pg_proc'), '|') FROM pg_proc p WHERE p.proname LIKE 'fn_shipments_%_sync'"))
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments"), before)
        b = Driver(b_project, url, "version-b-restored")
        self.addCleanup(b.stop)
        status, body = b.request("get", f"/api/v1/shipments/{after['id']}")
        self.assertEqual((status, body["reference"], body["label"]), (200, "after-drop", "after-drop"))
        b.stop()
        self.tool(d_project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'reference'"), "0")
        # a second contract of the same operation is a plan-level refusal, not a second drop
        again = plan_evolution(load_evolution(str(self.drop_contract(d_project, evidence_path, "drop-again.json"))), authorize_contract=True, confirm_irreversible=phrase)
        self.assertTrue(any(x.startswith("contract_operation_pending") for x in again.blockers), again.blockers)

    # -- B and A when the evolution is planned the wrong way round --------------- #

    def test_03_contract_before_retire_and_retire_without_evidence_are_refused_on_a_live_project(self) -> None:
        _a, b_project, db, url = self.stage_b("ordering", rows=50)
        ops = {c["column"]: c for c in read_manifest(str(b_project))["phased_changes"]}
        premature = self.drop_contract(b_project, self.root / "none.json", "ordering-premature.json")
        plan = plan_evolution(load_evolution(str(premature)), authorize_contract=True, confirm_irreversible="drop shipments.reference, shipments.weight_kg")
        self.assertTrue(any(x.startswith("contract_rename_retire_first") for x in plan.blockers), plan.blockers)
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(b_project)}, "authorization": {"contract": {"declared_by": "tests"}}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "retire_fields": [{"name": "label", "operation": ops["label"]["op_id"]}]}]}], "invariants": INVARIANTS}
        plan = plan_evolution(load_evolution(str(self.write("ordering-retire-blind.json", doc))))
        self.assertTrue(any(x.startswith("contract_evidence_valid") for x in plan.blockers), plan.blockers)
        # evidence from a database where the backfill did not run: the retire is refused by the measured state
        fresh = self.cluster.create_database("ordering_fresh")
        self.tool(b_project, fresh, "alembic", "upgrade", "head")
        self.sql("ordering_fresh", "ALTER TABLE shipments DISABLE TRIGGER ALL; INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'r' || g FROM generate_series(1, 20) g; ALTER TABLE shipments ENABLE TRIGGER ALL")
        code, evidence, _ = self.backfill(b_project, fresh, "verify", "--application-version", ops["label"]["application_version"], "--output", str(self.root / "ordering-evidence.json"), check=False)
        self.assertFalse(evidence["ok"])
        doc["authorization"]["contract"]["evidence"] = str(self.root / "ordering-evidence.json")
        plan = plan_evolution(load_evolution(str(self.write("ordering-retire-unverified.json", doc))))
        names = {x.split(":")[0] for x in plan.blockers}
        self.assertTrue({"retire_columns_agree", "retire_backfill_complete"} <= names, plan.blockers)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE label IS DISTINCT FROM reference"), "0", "the live database was never touched")


if __name__ == "__main__":
    unittest.main()
