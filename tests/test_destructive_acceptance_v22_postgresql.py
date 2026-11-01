"""Opt-in (``FA_DESIGN_PG=1``): acceptance hardening of the first destructive operation of the generator (v2.2,
increment 2) on a real, throwaway PostgreSQL. Negative tests only: nothing here adds a capability.

Four matrices. **Transition integrity** on the retire schema, statement by statement (old name only, new name only,
both equal, both different, partial updates, explicit NULLs, rollback, two sessions on one row, the old application's
writes after the retire). **Defaults**: a field with a declared default is renamed; the ambiguity a column default
creates for the sync trigger must be refused, not resolved. **Destructive dependencies**: every kind of object
PostgreSQL would drop together with the column or that would make ``DROP COLUMN … RESTRICT`` fail (normal and partial
indexes, unique and check constraints, foreign keys, views and materialized views, row-level-security policies,
generated columns, triggers of other code, functions naming the column, extended statistics) must be listed by the
evidence, must block the plan and must make the revision refuse on the live catalog with the schema intact.
**Atomicity and failures**: the checks, the lock and the drop share one transaction; a lock timeout, a backend killed
while waiting, a session of an older generation reconnecting between the plan and the upgrade, evidence that aged
between the plan and the upgrade, a downgrade that fails half-way, a downgrade queued behind traffic and the retry
after an aborted attempt. What this does not show: a distributed deployment; a network partition in the middle of the
drop statement itself (PostgreSQL rolls the transaction back on its side, the client sees an error).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from test_renames_v22_postgresql import EXAMPLES
from test_retire_contract_v22_postgresql import INVARIANTS, Driver, RetireContractCase

from _audit_support import SKILL_SCRIPTS
from _pg_support import OWNER, find_pg, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.model import TOOL_VERSION  # noqa: E402
from fastapi_design.blueprint.phased import confirmation_phrase  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402

ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
PARCELS_SPEC = EXAMPLES / "specs" / "parcels.json"

# (name, SQL that creates the dependency on shipments.reference, SQL that removes it, the kind of detection expected)
DEPENDENCIES: List[Tuple[str, str, str, str]] = [
    ("index", "CREATE INDEX ix_dep_reference ON shipments (reference)", "DROP INDEX ix_dep_reference", "catalog"),
    ("partial index", "CREATE INDEX ix_dep_partial ON shipments (id) WHERE reference <> ''", "DROP INDEX ix_dep_partial", "catalog"),
    ("unique constraint", "ALTER TABLE shipments ADD CONSTRAINT uq_dep_reference UNIQUE (reference)", "ALTER TABLE shipments DROP CONSTRAINT uq_dep_reference", "catalog"),
    ("check constraint", "ALTER TABLE shipments ADD CONSTRAINT ck_dep_reference CHECK (reference <> 'never')", "ALTER TABLE shipments DROP CONSTRAINT ck_dep_reference", "catalog"),
    (
        "foreign key",
        "ALTER TABLE shipments ADD CONSTRAINT uq_dep_fk UNIQUE (reference); CREATE TABLE dep_audit (id uuid PRIMARY KEY, ref varchar(40) REFERENCES shipments (reference))",
        "DROP TABLE dep_audit; ALTER TABLE shipments DROP CONSTRAINT uq_dep_fk",
        "catalog",
    ),
    ("view", "CREATE VIEW dep_view AS SELECT id, reference FROM shipments", "DROP VIEW dep_view", "catalog"),
    ("materialized view", "CREATE MATERIALIZED VIEW dep_matview AS SELECT id, reference FROM shipments", "DROP MATERIALIZED VIEW dep_matview", "catalog"),
    ("row-level-security policy", "CREATE POLICY dep_policy ON shipments USING (reference <> 'hidden')", "DROP POLICY dep_policy ON shipments", "catalog"),
    ("generated column", "ALTER TABLE shipments ADD COLUMN dep_upper text GENERATED ALWAYS AS (upper(reference)) STORED", "ALTER TABLE shipments DROP COLUMN dep_upper", "catalog"),
    (
        "trigger of other code (column list)",
        "CREATE FUNCTION dep_noop() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; CREATE TRIGGER dep_trigger BEFORE UPDATE OF reference ON shipments FOR EACH ROW EXECUTE FUNCTION dep_noop()",
        "DROP TRIGGER dep_trigger ON shipments; DROP FUNCTION dep_noop()",
        "catalog",
    ),
    (
        "trigger of other code (names the column in its body)",
        "CREATE FUNCTION dep_audit_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reference IS NULL THEN RAISE EXCEPTION 'no'; END IF; RETURN NEW; END $$; CREATE TRIGGER dep_audit BEFORE INSERT ON shipments FOR EACH ROW EXECUTE FUNCTION dep_audit_fn()",
        "DROP TRIGGER dep_audit ON shipments; DROP FUNCTION dep_audit_fn()",
        "code",
    ),
    (
        "function naming table and column",
        "CREATE FUNCTION dep_report() RETURNS bigint LANGUAGE plpgsql AS $$ BEGIN RETURN (SELECT count(*) FROM shipments WHERE reference <> ''); END $$",
        "DROP FUNCTION dep_report()",
        "code",
    ),
    ("extended statistics", "CREATE STATISTICS dep_stats (dependencies) ON reference, label FROM shipments", "DROP STATISTICS dep_stats", "catalog"),
]


@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the destructive acceptance against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class DestructiveAcceptanceTests(RetireContractCase):
    TIMINGS = "destructive-acceptance-timings.json"

    # -- helpers ------------------------------------------------------------- #

    def psql_session(self, db: str) -> subprocess.Popen:
        psql = find_pg("psql")
        assert psql
        session = subprocess.Popen([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-q"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: session.poll() is None and session.kill())
        return session

    def verbose_attempt(self, db: str, statement: str) -> Tuple[int, str]:
        """Like ``sql_attempt`` but with the SQLSTATE in the error output."""
        psql = find_pg("psql")
        assert psql
        proc = subprocess.run([psql, "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-At", "-c", statement], capture_output=True, text=True, env=self.cluster._env, timeout=120)
        return proc.returncode, proc.stderr.strip()

    def pair(self, db: str, row: str) -> str:
        return self.sql(db, f"SELECT coalesce(reference, '<null>') || '|' || coalesce(label, '<null>') || '|' || coalesce(weight_kg::text, '<null>') || '|' || coalesce(weight::text, '<null>') FROM shipments WHERE id = '{row}'")

    def schema_intact(self, db: str, head: str, reason: str) -> None:
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), head, reason)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "2", reason)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' AND tgenabled = 'O'"), "2", reason)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'fn_shipments_%_sync'"), "2", reason)

    def plan_blockers(self, project: Path, evidence: Path, name: str, **kw) -> List[str]:
        plan = plan_evolution(load_evolution(str(self.drop_contract(project, evidence, name))), **kw)
        return sorted({x.split(":")[0] for x in plan.blockers})

    def stage_d(self, label: str, rows: int = 300, **destructive: object) -> Dict[str, object]:
        """A, B and C on one database; A and B stopped; D (the destructive contract) applied to a copy of C but its
        revision not yet run on the shared database. Returns everything the atomicity and dependency matrices need."""
        a_project, b_project, db, url = self.stage_b(label, rows=rows)
        c_project, _report = self.stage_c(b_project, url, db, label, verify=False)
        c_ops = {x["column"]: x for x in read_manifest(str(c_project))["phased_changes"]}
        c_version = c_ops["label"]["application_version"]
        phrase = confirmation_phrase(list(c_ops.values()))
        evidence_path = self.root / f"{label}-evidence.json"
        code, evidence, _ = self.backfill(c_project, url, "verify", "--application-version", c_version, "--output", str(evidence_path))
        self.assertTrue(evidence["ok"], evidence["problems"])
        d_project = self.project_copy(c_project, f"{label}-d")
        report = self.evolve_with(d_project, self.drop_contract(d_project, evidence_path, f"{label}-drop.json", **destructive), None, verify=False, authorize_contract=True, confirm_irreversible=phrase)
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        head = self.sql(db, "SELECT version_num FROM alembic_version")
        return {"a": a_project, "b": b_project, "c": c_project, "d": d_project, "db": db, "url": url, "ops": c_ops, "version": c_version, "phrase": phrase, "evidence": evidence_path, "head": head}

    # -- 1. transition integrity ---------------------------------------------- #

    def test_01_every_write_pattern_on_the_retire_schema_keeps_the_pair_or_is_refused(self) -> None:
        _a, b_project, db, url = self.stage_b("matrix", rows=40)
        c_project, _ = self.stage_c(b_project, url, db, "matrix", verify=False)
        outcomes: Dict[str, str] = {}

        def insert(columns: str, values: str) -> Tuple[int, str, Optional[str]]:
            code, err = self.verbose_attempt(db, f"INSERT INTO shipments (id, {columns}) VALUES ('{{}}', {values})".format("00000000-0000-4000-8000-%012d" % (len(outcomes) + 1)))
            row = "00000000-0000-4000-8000-%012d" % (len(outcomes) + 1) if code == 0 else None
            return code, err, row

        # old name only / new name only
        code, _err, row = insert('reference, weight_kg', "'old-only', 1.5")
        self.assertEqual((code, self.pair(db, row)), (0, "old-only|old-only|1.50|1.50"))
        outcomes["insert old only"] = self.pair(db, row)
        code, _err, row2 = insert('label, weight', "'new-only', 2.5")
        self.assertEqual((code, self.pair(db, row2)), (0, "new-only|new-only|2.50|2.50"))
        outcomes["insert new only"] = self.pair(db, row2)
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET reference = 'old-upd' WHERE id = '{row}'")[0], 0)
        self.assertEqual(self.pair(db, row), "old-upd|old-upd|1.50|1.50")
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET label = 'new-upd', weight = 3 WHERE id = '{row}'")[0], 0)
        self.assertEqual(self.pair(db, row), "new-upd|new-upd|3.00|3.00")
        # both names, equal values: accepted, nothing to mirror
        code, _err, row3 = insert('reference, label', "'same', 'same'")
        self.assertEqual((code, self.pair(db, row3)), (0, "same|same|<null>|<null>"))
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET reference = 'both', label = 'both' WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        # both names, different values: refused with SQLSTATE 23514, the row untouched, nothing inserted
        code, err, _none = insert('reference, label', "'one', 'two'")
        self.assertNotEqual(code, 0)
        self.assertIn("23514", err)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IN ('one', 'two') OR label IN ('one', 'two')"), "0")
        code, err = self.verbose_attempt(db, f"UPDATE shipments SET reference = 'x', label = 'y' WHERE id = '{row3}'")
        self.assertNotEqual(code, 0)
        self.assertIn("23514", err)
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        code, err = self.verbose_attempt(db, f"UPDATE shipments SET weight_kg = 7, weight = 8 WHERE id = '{row3}'")
        self.assertNotEqual(code, 0)
        self.assertIn("23514", err)
        outcomes["both names different"] = "refused 23514, row untouched"
        # a partial update that touches neither column of a pair leaves the pair alone
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET weight = 9 WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.pair(db, row3), "both|both|9.00|9.00")
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET created_at = now() WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.pair(db, row3), "both|both|9.00|9.00")
        # explicit NULL: propagates on the nullable pair, refused on the NOT NULL pair (either name)
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET weight_kg = NULL WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET weight = 4 WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.sql_attempt(db, f"UPDATE shipments SET weight = NULL WHERE id = '{row3}'")[0], 0)
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        for statement in (f"UPDATE shipments SET reference = NULL WHERE id = '{row3}'", f"UPDATE shipments SET label = NULL WHERE id = '{row3}'", "INSERT INTO shipments (id) VALUES (gen_random_uuid())"):
            code, err = self.verbose_attempt(db, statement)
            self.assertNotEqual(code, 0, statement)
            self.assertIn("23502", err, statement)  # not_null_violation
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        outcomes["explicit NULL"] = "nullable pair both NULL; NOT NULL pair refused 23502"
        # rollback leaves both columns untouched
        self.sql(db, f"BEGIN; UPDATE shipments SET label = 'rolled' WHERE id = '{row3}'; UPDATE shipments SET weight_kg = 1 WHERE id = '{row3}'; ROLLBACK")
        self.assertEqual(self.pair(db, row3), "both|both|<null>|<null>")
        # two sessions on one row: the second waits for the first; the last committed write wins on both columns
        first = self.psql_session(db)
        assert first.stdin
        first.stdin.write(f"BEGIN;\nUPDATE shipments SET reference = 's1' WHERE id = '{row3}';\n")
        first.stdin.flush()
        time.sleep(0.3)
        second = subprocess.Popen([find_pg("psql") or "psql", "-h", "127.0.0.1", "-p", str(self.cluster.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-c", f"UPDATE shipments SET label = 's2' WHERE id = '{row3}'"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.cluster._env)
        time.sleep(0.5)
        self.assertIsNone(second.poll(), "the second writer waits for the row lock")
        first.stdin.write("COMMIT;\n")
        first.stdin.flush()
        first.stdin.close()
        first.wait(timeout=30)
        second.wait(timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr.read())
        self.assertEqual(self.pair(db, row3), "s2|s2|<null>|<null>")
        outcomes["two sessions, one row"] = self.pair(db, row3)
        # the old application after the retire: old-name writes still mirrored (nothing lost), a NULL refused by the new NOT NULL
        a = Driver(_a, url, "version-a")
        self.addCleanup(a.stop)
        status, by_a = a.request("post", "/api/v1/shipments", {"reference": "a-after-retire", "weight_kg": "6.00"})
        self.assertEqual(status, 201, by_a)
        self.assertEqual(self.pair(db, by_a["id"]), "a-after-retire|a-after-retire|6.00|6.00")
        status, body = a.request("patch", f"/api/v1/shipments/{by_a['id']}", {"weight_kg": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.pair(db, by_a["id"]), "a-after-retire|a-after-retire|<null>|<null>")
        c = Driver(c_project, url, "version-c")
        self.addCleanup(c.stop)
        status, seen = c.request("get", f"/api/v1/shipments/{by_a['id']}")
        self.assertEqual((status, seen["label"], seen["weight"]), (200, "a-after-retire", None))
        outcomes["A after retire"] = "mirrored; C reads it"
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IS DISTINCT FROM label OR weight_kg IS DISTINCT FROM weight"), "0")
        self.save("acceptance-transition-matrix.json", outcomes)

    # -- 2. defaults ---------------------------------------------------------- #

    def test_02_a_rename_of_a_field_with_a_default_is_refused_because_the_trigger_cannot_tell_a_default_from_a_value(self) -> None:
        spec_doc = json.loads(PARCELS_SPEC.read_text(encoding="utf-8"))
        spec_doc.update(name="tickets", package="tickets", description="tickets with a defaulted state")
        spec_doc["functional"]["modules"] = [{"name": "tickets", "description": "tickets", "entities": [{"name": "Ticket", "fields": [{"name": "code", "type": "str", "max_length": 20}, {"name": "state", "type": "str", "max_length": 10, "default": "open"}], "operations": ["create", "get", "list", "update"]}]}]
        spec_path = self.write("tickets-spec.json", spec_doc)
        spec = design.load_spec(str(spec_path))
        project = self.root / "tickets"
        design.generate(spec, design.build(spec, design.recommend(spec)), str(project), tool_version=TOOL_VERSION)
        rename = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "tickets", "entities": [{"name": "Ticket", "rename_fields": [{"from": "state", "to": "status", "strategy": "expand_contract"}]}]}], "invariants": INVARIANTS}
        plan = plan_evolution(load_evolution(str(self.write("tickets-rename.json", rename))))
        names = {x.split(":")[0] for x in plan.blockers}
        if "rename_defaulted_column_unsupported" in names:
            self.save("acceptance-defaults.json", {"outcome": "refused at plan time", "blockers": plan.blockers})
            return
        # Not refused: show what the column default does to an explicit value written through one name only.
        db = "tickets"
        url = self.cluster.create_database(db)
        report = run_evolution(str(self.root / "tickets-rename.json"), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.tool(project, url, "alembic", "upgrade", "head")
        self.sql(db, "INSERT INTO tickets (id, code, status) VALUES (gen_random_uuid(), 'T-1', 'closed')")
        observed = self.sql(db, "SELECT state || '|' || status FROM tickets WHERE code = 'T-1'")
        self.save("acceptance-defaults.json", {"outcome": "not refused", "insert status='closed' observed state|status": observed})
        self.fail(f"a rename of a defaulted field was planned and an explicit value written through the new name became {observed!r}: the default of the old column was taken for a value")

    # -- 3. destructive dependencies ------------------------------------------ #

    def test_03_every_dependency_kind_blocks_the_plan_and_makes_the_revision_refuse_with_the_schema_intact(self) -> None:
        s = self.stage_d("deps", rows=200)
        db, url, d_project, c_project = str(s["db"]), str(s["url"]), s["d"], s["c"]
        head = str(s["head"])
        matrix: Dict[str, Dict[str, object]] = {}
        for name, create, remove, kind in DEPENDENCIES:
            with self.subTest(name):
                self.sql(db, create)
                try:
                    code, evidence, _ = self.backfill(c_project, url, "verify", "--application-version", str(s["version"]), "--output", str(self.root / "deps-evidence-dep.json"), check=False)
                    entry = evidence["operations"][s["ops"]["label"]["op_id"]]
                    listed = list(entry["unmanaged_dependents"]) + [t["name"] for t in entry["other_triggers"] if t["mentions_column"]] + list(entry["functions_mentioning"])
                    self.assertTrue(listed, f"{name}: the evidence lists nothing for the old column")
                    blockers = self.plan_blockers(c_project, self.root / "deps-evidence-dep.json", "deps-contract-dep.json", authorize_contract=True, confirm_irreversible=str(s["phrase"]))
                    self.assertIn("contract_dependencies_reviewed", blockers, name)
                    proc = self.tool(d_project, url, "alembic", "upgrade", "head", check=False)
                    self.assertNotEqual(proc.returncode, 0, f"{name}: the revision dropped the column with a dependency in place")
                    output = proc.stderr + proc.stdout
                    expected = "dependents the tool does not manage" if kind == "catalog" else "named by code the tool does not manage"
                    self.assertIn(expected, output, name)
                    self.schema_intact(db, head, name)
                    matrix[name] = {"evidence": listed, "plan": "contract_dependencies_reviewed", "revision": expected, "schema": "intact"}
                finally:
                    self.sql(db, remove)
        self.save("acceptance-dependency-matrix.json", matrix)
        # with every dependency gone the same revision applies
        code, evidence, _ = self.backfill(c_project, url, "verify", "--application-version", str(s["version"]), "--output", str(s["evidence"]))
        self.assertTrue(evidence["ok"], evidence["problems"])
        self.assertEqual(self.plan_blockers(c_project, Path(str(s["evidence"])), "deps-contract-clean.json", authorize_contract=True, confirm_irreversible=str(s["phrase"])), [])
        self.tool(d_project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "0")

    # -- 4. atomicity and failures -------------------------------------------- #

    def test_04_the_checks_the_lock_and_the_drop_fail_together_and_a_retry_succeeds(self) -> None:
        s = self.stage_d("atomic", rows=300, ignore_sessions=["psql"])  # the lock holders of this test are psql consoles, named on purpose
        db, url, d_project, b_project, c_project = str(s["db"]), str(s["url"]), s["d"], s["b"], s["c"]
        head = str(s["head"])
        drop_revision = read_manifest(str(d_project))["migrations"][-1]["revision"]
        # (a) a lock holder: the revision fails after its lock timeout, not after the holder
        holder = self.hold_lock(db, "true")
        started = time.time()
        proc = self.tool(d_project, url, "alembic", "upgrade", "head", check=False)
        elapsed = time.time() - started
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lock timeout", (proc.stderr + proc.stdout).lower())
        self.assertLess(elapsed, 60)
        self.schema_intact(db, head, "lock timeout")
        self.release(holder)
        # (b) the backend is killed while it waits for the lock: the client sees an error, the database holds nothing of it
        holder = self.hold_lock(db, "true")
        upgrade = subprocess.Popen(["uv", "run", "--group", "dev", "alembic", "upgrade", "head"], cwd=str(d_project), env=self.env(d_project, url), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 20
        pid = ""
        while time.time() < deadline and not pid:
            pid = self.sql(db, "SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query ILIKE 'LOCK TABLE%' LIMIT 1")
            time.sleep(0.1)
        self.assertTrue(pid, "the revision never reached LOCK TABLE")
        self.assertEqual(self.sql(db, f"SELECT pg_terminate_backend({pid})"), "t")
        out, err = upgrade.communicate(timeout=60)
        self.assertNotEqual(upgrade.returncode, 0)
        self.assertTrue(any(word in (out + err).lower() for word in ("terminat", "connection was closed", "closed in the middle")), (out + err)[-600:])
        self.release(holder)
        self.schema_intact(db, head, "backend terminated while waiting")
        # (c) a session of the transitional generation reconnects between the plan and the upgrade
        b = Driver(b_project, url, "version-b-late")
        self.addCleanup(b.stop)
        self.assertEqual(b.request("get", "/api/v1/shipments?limit=1")[0], 200)
        proc = self.tool(d_project, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0, "the revision dropped the column while a generation that maps it was connected")
        self.assertIn("session", (proc.stderr + proc.stdout).lower())
        self.schema_intact(db, head, "B reconnected")
        b.stop()
        time.sleep(0.5)
        # (d) rows written and the trigger kept the pair: the retry succeeds once nothing stands in the way
        c = Driver(c_project, url, "version-c")
        self.addCleanup(c.stop)
        for i in range(3):
            self.assertEqual(c.request("post", "/api/v1/shipments", {"label": f"before-drop-{i}"})[0], 201)
        started = time.time()
        self.tool(d_project, url, "alembic", "upgrade", "head")
        self.timed("atomic_drop_upgrade_s", started)
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), drop_revision)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "0")
        self.assertEqual(c.request("get", "/api/v1/shipments?limit=2")[0], 200)
        # (e) a downgrade that fails half-way leaves nothing behind: a trigger of the same name already exists
        op_id = str(s["ops"]["label"]["op_id"])
        self.sql(db, f"CREATE FUNCTION dep_clash() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; CREATE TRIGGER trg_shipments_{op_id}_sync BEFORE UPDATE ON shipments FOR EACH ROW EXECUTE FUNCTION dep_clash()")
        proc = self.tool(d_project, url, "alembic", "downgrade", "-1", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already exists", proc.stderr + proc.stdout)
        self.assertEqual(self.sql(db, "SELECT version_num FROM alembic_version"), drop_revision, "a half-done downgrade must not move the version")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name IN ('reference', 'weight_kg')"), "0", "the recreated columns of a failed downgrade must be gone")
        self.sql(db, f"DROP TRIGGER trg_shipments_{op_id}_sync ON shipments; DROP FUNCTION dep_clash()")
        # (f) a downgrade queued behind traffic fails fast instead of waiting indefinitely
        holder = self.hold_lock(db, "true")
        started = time.time()
        proc = self.tool(d_project, url, "alembic", "downgrade", "-1", check=False)
        elapsed = time.time() - started
        self.release(holder)
        self.assertNotEqual(proc.returncode, 0, "the downgrade must not proceed while rows are locked")
        self.assertIn("lock timeout", (proc.stderr + proc.stdout).lower())
        self.assertLess(elapsed, 60)
        self.assertEqual(self.sql(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'shipments' AND column_name = 'reference'"), "0")
        # (g) the retry of the downgrade succeeds and restores the pair
        self.tool(d_project, url, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM shipments WHERE reference IS DISTINCT FROM label OR weight_kg IS DISTINCT FROM weight"), "0")
        self.assertEqual(self.sql(db, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_shipments_%_sync' AND tgenabled = 'O'"), "2")
        self.tool(d_project, url, "alembic", "upgrade", "head")

    def test_05_evidence_that_aged_between_the_plan_and_the_upgrade_is_refused_by_the_revision(self) -> None:
        s = self.stage_d("aged", rows=50)
        db, url = str(s["db"]), str(s["url"])
        # the contract tolerated 0.001 h of evidence age: planned at once (fresh), applied minutes later it must refuse
        aged_contract = json.loads((self.root / "aged-drop.json").read_text(encoding="utf-8"))
        aged_contract["authorization"]["contract"]["max_evidence_age_hours"] = 0.001
        path = self.write("aged-drop-tight.json", aged_contract)
        code, evidence, _ = self.backfill(s["c"], url, "verify", "--application-version", str(s["version"]), "--output", str(s["evidence"]))
        d2 = self.project_copy(s["c"], "aged-d2")
        aged_contract["project"]["path"] = str(d2)
        path = self.write("aged-drop-tight.json", aged_contract)
        report = run_evolution(str(path), EvolveOptions(apply=True, verify=False, authorize_contract=True, confirm_irreversible=str(s["phrase"])))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        time.sleep(5)
        proc = self.tool(d2, url, "alembic", "upgrade", "head", check=False)
        self.assertNotEqual(proc.returncode, 0, "the revision ran on evidence older than the contract allows")
        self.assertIn("evidence", (proc.stderr + proc.stdout).lower())
        self.schema_intact(db, str(s["head"]), "aged evidence")


if __name__ == "__main__":
    unittest.main()
