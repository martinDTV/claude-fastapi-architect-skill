"""Increment 5 of v2.2, scenarios C and D on a real PostgreSQL: how the files the journal writes, the state of the
database Alembic records and the migration evidence are three different facts that nothing here infers from one
another.

Scenario C: the phased expand of ``parcels`` is interrupted in the middle of its publication; the journal is
pending, the database head has not moved and no evidence exists; the recovery finishes the files, the project's own
``alembic upgrade head`` moves the database, the backfill runs and writes the evidence; a revision that fails on the
database (a column that already exists) leaves ``alembic_version`` where it was and no half-applied revision.
Scenario D: a destructive contract that is refused leaves the tree and the database intact.

Opt-in like the other PostgreSQL modules: ``FA_DESIGN_PG=1``, PostgreSQL binaries and ``uv`` on the PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict

from _audit_support import REPO_ROOT, SKILL_SCRIPTS
from _pg_support import ThrowawayCluster, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_common.artefacts import DEFAULT_WALK_SKIP, walk_files  # noqa: E402
from fastapi_design import evolution, journal, locking  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVIDENCE_DIR = os.environ.get("FA_DESIGN_PG_EVIDENCE")
EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
EXPAND = EXAMPLES / "evolutions" / "parcels-expand-carrier.json"
CONTRACT_LABEL = EXAMPLES / "evolutions" / "parcels-contract-label.json"
ROWS = 300


def _uv_env() -> Dict[str, str]:
    home = Path.home()
    return {"UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(home / ".cache" / "uv")), "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR", str(home / ".local" / "share" / "uv" / "python"))}


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the recovery scenarios against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class RecoveryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ThrowawayCluster().__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="fa-recovery-pg-"))
        cls.timings: Dict[str, float] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / "recovery-timings.json").write_text(json.dumps(cls.timings, indent=2, sort_keys=True), encoding="utf-8")

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

    def sql(self, db: str, statement: str) -> str:
        return self.cluster.psql(statement, db=db)

    def contract(self, source: Path, project: Path, name: str) -> Path:
        doc = json.loads(source.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def timed(self, label: str, started: float) -> None:
        self.timings[label] = round(time.time() - started, 3)

    def test_01_scenario_c_an_interrupted_phased_evolution_keeps_files_database_and_evidence_apart(self) -> None:
        project = self.project_copy("parcels")
        db = "parcels_c"
        url = self.cluster.create_database(db)
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("baseline_upgrade_s", started)
        head_before = self.sql(db, "SELECT version_num FROM alembic_version")
        self.sql(db, f"INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'ref-' || g FROM generate_series(1, {ROWS}) g")
        before = tree(project)
        contract = self.contract(EXPAND, project, "expand.json")
        # 1. the publication dies after four files: the journal is pending, the database and the evidence untouched
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, faults=journal.FaultInjector(points=["after_publish:4"])))
        self.assertEqual(report["applied"]["state"], "interrupted", report["plan"]["blockers"])
        self.assertEqual(report["state"], "interrupted")
        self.assertNotIn("database", report["applied"], "an interrupted write claims nothing about the database")
        pending = journal.pending_operations(project)
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0].published), 4)
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head_before, "the files moved, the database did not")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'carrier'"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'parcels_backfill_progress'"), "0")
        self.assertFalse(list(project.glob("**/migration-evidence*.json")), "no evidence exists before verify ran")
        blocked = evolution.plan_evolution(evolution.load_evolution(str(contract)))
        self.assertEqual([b.split(":")[0] for b in blocked.blockers], ["no_pending_operation"])
        # 2. forward recovery finishes the files; the database is still where it was
        outcome = journal.recover(project, pending[0], strategy="forward")
        self.assertEqual(outcome.state, "completed")
        self.assertEqual(journal.pending_operations(project), [])
        manifest = read_manifest(str(project))
        phased = {c["column"]: c for c in manifest["phased_changes"]}
        self.assertIn("carrier", phased)
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head_before)
        self.assertEqual(evolution.plan_evolution(evolution.load_evolution(str(contract))).state, "noop", "the recovered tree is the applied tree")
        # 3. a revision that fails on the database leaves alembic_version and the schema exactly as they were
        self.sql(db, "ALTER TABLE shipments ADD COLUMN carrier VARCHAR(80)")
        failed = self.tool(project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(failed.returncode, 0, "the expand revision cannot add a column that already exists")
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head_before, "transactional DDL: the failed revision moved nothing")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'parcels_backfill_progress'"), "0", "nothing of the failed revision stayed behind")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_class WHERE relname = 'ix_shipments_carrier'"), "0")
        self.sql(db, "ALTER TABLE shipments DROP COLUMN carrier")
        # 4. the project's own upgrade applies expand and index; the backfill runs and writes the evidence
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("expand_and_index_upgrade_s", started)
        head_after = self.sql(db, "SELECT version_num FROM alembic_version")
        self.assertNotEqual(head_after, head_before)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE carrier IS NULL"), str(ROWS))
        op_id = phased["carrier"]["op_id"]
        proc = self.tool(project, url, "python", "-m", "parcels.ops.backfill", "run", op_id, "--batch-size", "100")
        self.assertEqual(json.loads(proc.stdout)["state"], "complete", proc.stdout[-500:])
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE carrier IS NULL"), "0")
        evidence = self.root / "evidence.json"
        proc = self.tool(project, url, "python", "-m", "parcels.ops.backfill", "verify", "--output", str(evidence))
        self.assertTrue(evidence.is_file())
        doc = json.loads(evidence.read_text())
        self.assertTrue(doc.get("schema", "").startswith("fastapi-architect/migration-evidence/"))
        # three facts, three places: the journal is empty, alembic_version moved, the evidence names this database
        self.assertEqual(journal.pending_operations(project), [])
        self.assertIsNone(locking.inspect(project))
        self.assertNotEqual(before, tree(project))
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head_after)
        # 5. an apply report of a schema-changing plan says so explicitly
        second = self.project_copy("parcels_report")
        report = evolution.run_evolution(str(self.contract(EXPAND, second, "expand2.json")), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assertEqual((report["applied"]["database"]["migration_required"], report["applied"]["database"]["applied_by_this_tool"]), (True, False))
        self.assertIn("alembic upgrade head", report["applied"]["database"]["status"])

    def test_02_scenario_d_a_refused_destructive_contract_leaves_tree_and_database_intact(self) -> None:
        project = self.project_copy("parcels_d")
        db = "parcels_d"
        url = self.cluster.create_database(db)
        self.tool(project, url, "alembic", "upgrade", "head")
        head = self.sql(db, "SELECT version_num FROM alembic_version")
        columns = self.sql(db, "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name = 'shipments'")
        self.sql(db, f"INSERT INTO shipments (id, reference) SELECT gen_random_uuid(), 'ref-' || g FROM generate_series(1, {ROWS}) g")
        before = tree(project)
        contract = self.contract(CONTRACT_LABEL, project, "contract-label.json")
        for options in (evolution.EvolveOptions(apply=True, verify=False), evolution.EvolveOptions(apply=True, verify=False, authorize_contract=True, confirm_irreversible="drop shipments.label")):
            try:
                report = evolution.run_evolution(str(contract), options)
                state, applied = report["state"], report["applied"]["state"]
            except evolution.EvolutionError:
                state, applied = "blocked", "not_applied"
            self.assertEqual((state, applied), ("blocked", "not_applied"))
            self.assertEqual(tree(project), before, "the tree is untouched by a refused destructive contract")
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head)
        self.assertEqual(self.sql(db, "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name = 'shipments'"), columns)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments"), str(ROWS))
        self.assertEqual(journal.pending_operations(project), [])
        self.assertIsNone(locking.inspect(project))
        # the project's own upgrade is a no-op: nothing was generated for the database to apply
        self.tool(project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head)


if __name__ == "__main__":
    unittest.main()
