"""v2.1, increment 2: phased migrations (expand, backfill, verify, contract) without a database.

What is proven here, on copies of the committed examples and on fresh generations: the evolution contract 1.2
accepts phased fields, concurrent indexes, contract fields and the contract authorization and rejects everything
that would smuggle SQL or weaken a phase; the planner derives stable operation ids and deterministic revisions,
renders pending columns nullable, generates the expand and index revisions, the runner, its definitions and its
tests; the generated code compiles, carries no SQL from the contract and passes the project's own Ruff configuration;
the contract phase is refused without explicit authorization, without evidence, with stale, mismatched or incomplete
evidence, and planned only when every precondition holds; declared, applied and verified states are kept apart; and
contracts 1.0 and 1.1 keep their meaning. The behaviour against a real PostgreSQL is in
``tests/test_migrations_v21_postgresql.py``.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design import migrations  # noqa: E402
from fastapi_design.blueprint.phased import EVIDENCE_SCHEMA, definition_checksum, index_name, operation_id  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution, validate_evolution  # noqa: E402
from fastapi_design.generator import MANIFEST_SCHEMA, read_manifest  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
ORDERS = EXAMPLES / "generated" / "orders-platform"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
PARCELS_SPEC = EXAMPLES / "specs" / "parcels.json"
PARCELS_SNAPSHOT = EXAMPLES / "generated" / "parcels"
EXPAND_EXAMPLE = EXAMPLES / "evolutions" / "parcels-expand-carrier.json"
CONTRACT_EXAMPLE = EXAMPLES / "evolutions" / "parcels-contract-carrier.json"
PY = sys.executable


def expand_contract(project: Path, **extra) -> Dict[str, Any]:
    doc = {
        "schema": EVOLUTION_SCHEMA,
        "project": {"path": str(project)},
        "modify_modules": [
            {
                "name": "customers",
                "entities": [
                    {
                        "name": "Customer",
                        "add_fields": [
                            {"name": "segment", "type": "str", "max_length": 20, "required": True, "backfill": {"strategy": "constant", "value": "unknown"}},
                            {"name": "external_ref", "type": "uuid", "required": True, "backfill": {"strategy": "uuid"}},
                        ],
                        "add_indexes": [{"columns": ["segment"]}],
                    }
                ],
            }
        ],
    }
    doc.update(extra)
    return doc


def evidence_for(manifest: Dict[str, Any], *, generated_at: Optional[datetime] = None, pending_rows: int = 0, state: str = "complete", application_version: Optional[str] = "manifest", current: Optional[List[str]] = None, index_valid: bool = True, index_present: bool = True) -> Dict[str, Any]:
    head = manifest["migrations"][-1]["revision"]
    operations = {}
    for change in manifest["phased_changes"]:
        operations[change["op_id"]] = {"table": change["table"], "column": change["column"], "state_declared": change["state_declared"], "column_present": True, "nullable": True, "pending_rows": pending_rows, "progress": {"state": state, "rows_processed": 10, "batches": 1, "passes": 1, "last_key": None, "checksum": definition_checksum(change)}, "backfill_complete": state == "complete" and pending_rows == 0}
    version = manifest["phased_changes"][0]["application_version"] if application_version == "manifest" else application_version
    return {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "expected_head": head,
        "application_version_declared": version,
        "database": {"name": "test", "user": "fa_owner", "server_version": "PostgreSQL 16", "bypasses_rls": True},
        "alembic": {"current": current if current is not None else [head], "matches_expected_head": True},
        "operations": operations,
        "indexes": [{"name": i["name"], "table": i["table"], "present": index_present, "valid": index_valid and index_present} for i in manifest["concurrent_indexes"]],
        "problems": [],
        "ok": True,
    }


class ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-phased-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target

    def write(self, name: str, doc: Dict[str, Any]) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def expanded_orders(self) -> Path:
        project = self.copy(ORDERS, "orders")
        report = run_evolution(str(self.write("expand.json", expand_contract(project))), EvolveOptions(apply=True, verify=True))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"] or report.get("verification", {}).get("reasons"))
        return project

    def contract_doc(self, project: Path, manifest: Dict[str, Any], **auth) -> Dict[str, Any]:
        ops = {c["column"]: c["op_id"] for c in manifest["phased_changes"]}
        contract: Dict[str, Any] = {"declared_by": "tests", "evidence": "evidence.json"}
        contract.update(auth)
        return {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "contract_fields": [{"name": "segment", "operation": ops["segment"]}]}]}], "authorization": {"contract": contract}}


# --------------------------------------------------------------------------- #
# contract 1.2
# --------------------------------------------------------------------------- #


class ContractValidationTests(unittest.TestCase):
    def errors(self, doc: Dict[str, Any]) -> List[str]:
        contract, errors = validate_evolution(doc, "test")
        return errors

    def base(self, **field) -> Dict[str, Any]:
        f = {"name": "segment", "type": "str", "max_length": 20, "required": True, "backfill": {"strategy": "constant", "value": "x"}}
        f.update(field)
        return {"schema": EVOLUTION_SCHEMA, "project": {"path": "p"}, "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_fields": [f]}]}]}

    def test_the_examples_validate(self) -> None:
        for path in (EXPAND_EXAMPLE, CONTRACT_EXAMPLE):
            contract = load_evolution(str(path))
            self.assertEqual(contract.schema, "fastapi-architect/evolution/1.2")  # the phased examples stay 1.2; 1.3 (v2.2) adds renames
        contract = load_evolution(str(CONTRACT_EXAMPLE))
        self.assertEqual(contract.authorization["contract"]["max_evidence_age_hours"], 24.0)
        self.assertEqual(self.errors(self.base()), [])

    def test_backfill_declarations_are_typed_and_never_sql(self) -> None:
        cases = {
            "sql strategy": (self.base(backfill={"strategy": "sql", "value": "1"}), "backfill.strategy"),
            "expression key": (self.base(backfill={"strategy": "constant", "value": "x", "expression": "now()"}), "unknown property"),
            "constant type mismatch": (self.base(type="int", backfill={"strategy": "constant", "value": "ten"}), "not a literal of type 'int'"),
            "constant missing value": (self.base(backfill={"strategy": "constant"}), "backfill.value: required"),
            "copy_column onto itself": (self.base(backfill={"strategy": "copy_column", "source": "segment"}), "cannot copy a column onto itself"),
            "copy_column without source": (self.base(backfill={"strategy": "copy_column"}), "backfill.source"),
            "uuid strategy on a str": (self.base(backfill={"strategy": "uuid"}), "needs a field of type 'uuid'"),
            "now strategy on a str": (self.base(backfill={"strategy": "now"}), "needs a field of type 'datetime'"),
            "default with backfill": (self.base(default="x"), "a phased field has no 'default'"),
            "reference with backfill": (self.base(type="uuid", references="orders.Order", backfill={"strategy": "uuid"}), "a reference cannot be backfilled"),
            "multi-column index": ({**self.base(), "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_indexes": [{"columns": ["a", "b"]}]}]}]}, "one column per concurrent index"),
            "index with sql": ({**self.base(), "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_indexes": [{"columns": ["lower(email)"]}]}]}]}, "expected a list of field names"),
            "contract field shape": ({**self.base(), "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "contract_fields": [{"name": "segment", "operation": "not-hex"}]}]}]}, "16-hex operation id"),
            "authorization without declared_by": ({**self.base(), "authorization": {"contract": {"evidence": "e.json"}}}, "declared_by: required"),
            "authorization with a negative age": ({**self.base(), "authorization": {"contract": {"declared_by": "x", "max_evidence_age_hours": -1}}}, "positive number"),
        }
        for label, (doc, fragment) in cases.items():
            with self.subTest(case=label):
                errors = self.errors(doc)
                self.assertTrue(any(fragment in e for e in errors), (label, errors))

    def test_older_schemas_keep_their_meaning(self) -> None:
        doc = self.base()
        doc["schema"] = "fastapi-architect/evolution/1.1"
        self.assertTrue(any("needs schema fastapi-architect/evolution/1.2" in e for e in self.errors(doc)))
        doc = {"schema": "fastapi-architect/evolution/1.1", "project": {"path": "p"}, "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_indexes": [{"columns": ["x"]}]}]}]}
        self.assertTrue(any("add_indexes: needs schema" in e for e in self.errors(doc)))
        doc = {"schema": "fastapi-architect/evolution/1.1", "project": {"path": "p"}, "authorization": {"contract": {"declared_by": "x"}}, "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_operations": ["update"]}]}]}
        self.assertTrue(any("authorization: needs schema" in e for e in self.errors(doc)))
        plain = {"schema": "fastapi-architect/evolution/1.1", "project": {"path": "p"}, "modify_modules": [{"name": "customers", "entities": [{"name": "Customer", "add_fields": [{"name": "phone", "type": "str", "required": False}]}]}]}
        self.assertEqual(self.errors(plain), [])
        for example in ("inventory-add-suppliers.json", "orders-add-customer-columns.json"):
            self.assertIsNotNone(load_evolution(str(EXAMPLES / "evolutions" / example)))

    def test_operation_ids_and_checksums_are_stable(self) -> None:
        a = operation_id("shipments", "carrier", "constant", {"value": "unknown"}, "8154db01f010")
        self.assertEqual(a, "66636e4277641692")
        self.assertEqual(operation_id("shipments", "tracking_token", "uuid", {}, "8154db01f010"), "5821fb0e87d4b355")
        self.assertNotEqual(a, operation_id("shipments", "carrier", "constant", {"value": "other"}, "8154db01f010"))
        self.assertNotEqual(a, operation_id("shipments", "carrier", "constant", {"value": "unknown"}, "other-head"))
        change = {"table": "t", "column": "c", "field_type": "str", "strategy": "constant", "params": {"value": "x"}}
        self.assertEqual(definition_checksum(change), definition_checksum(dict(change, state_declared="contract")))
        self.assertNotEqual(definition_checksum(change), definition_checksum(dict(change, params={"value": "y"})))
        self.assertEqual(index_name("shipments", ["carrier"]), "ix_shipments_carrier")


# --------------------------------------------------------------------------- #
# planning and generation
# --------------------------------------------------------------------------- #


class ExpandPlanningTests(ProjectCase):
    def test_the_expand_plan_declares_phases_and_generates_the_runner(self) -> None:
        project = self.copy(ORDERS, "orders")
        contract = load_evolution(str(self.write("expand.json", expand_contract(project))))
        plan = plan_evolution(contract)
        self.assertEqual(plan.state, "planned", plan.blockers)
        kinds = [(c["kind"], c["classification"]) for c in plan.changes]
        self.assertEqual(kinds, [("add_field", "compatible"), ("add_field", "compatible"), ("add_index", "compatible")])
        self.assertTrue(all("phased" in c["detail"] for c in plan.changes if c["kind"] == "add_field"))
        phases = plan.migration["phases"]
        self.assertEqual([p["phase"] for p in phases], ["expand", "index"])
        self.assertTrue(phases[0]["transactional"] and not phases[1]["transactional"])
        self.assertTrue(all(p["reversible"] for p in phases))
        self.assertEqual(plan.migration["head_after"], phases[1]["revision"])
        self.assertEqual(len(plan.migration["backfills"]), 2)
        backfill = plan.migration["backfills"][0]
        self.assertTrue(backfill["run"].startswith("python -m orders_platform.ops.backfill run "))
        self.assertIn("application_version", backfill)
        self.assertIn("declared: expand", backfill["state"])
        created = set(plan.create)
        self.assertIn("src/orders_platform/ops/backfill.py", created)
        self.assertIn("src/orders_platform/ops/backfills.py", created)
        self.assertIn("tests/test_backfill.py", created)
        self.assertEqual(len([c for c in created if c.startswith("migrations/versions/")]), 2)
        self.assertIn("migrations/env.py", plan.modify)
        self.assertIn("tests/test_migrations.py", plan.modify)
        expand_file = next(c for c in created if "_expand_" in c)
        expand_source = plan.files[expand_file]
        self.assertIn('op.add_column("customers", sa.Column("segment", sa.String(length=20), nullable=True))', expand_source)
        self.assertNotIn("server_default", expand_source.split("def upgrade")[1].split("op.create_table")[0])
        self.assertIn('"orders_platform_backfill_progress"', expand_source)
        index_file = next(c for c in created if "_concurrent_" in c)
        index_source = plan.files[index_file]
        self.assertIn("autocommit_block", index_source)
        self.assertIn("CREATE {kind} CONCURRENTLY", index_source)
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", index_source)
        self.assertIn("exists with a different definition", index_source)
        self.assertNotIn("IF NOT EXISTS", index_source.split("def upgrade")[1])
        models = plan.files["src/orders_platform/modules/customers/models.py"]
        self.assertIn("segment: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)", models)
        self.assertIn("external_ref: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)", models)
        schemas = plan.files["src/orders_platform/modules/customers/schemas.py"]
        create_block = schemas.split("class CustomerCreate")[1].split("class ")[0]
        read_block = schemas.split("class CustomerRead")[1]
        self.assertIn("segment: str", create_block)
        self.assertIn("segment: str | None", read_block)
        for rel, content in plan.files.items():
            if rel.endswith(".py"):
                ast.parse(content, rel)
        self.assertTrue(any("declared, not applied" in r for r in plan.risks))
        again = plan_evolution(load_evolution(str(self.write("expand2.json", expand_contract(project)))))
        self.assertEqual(again.files, plan.files, "planning is deterministic")

    def test_no_sql_from_the_contract_reaches_the_generated_code(self) -> None:
        project = self.copy(ORDERS, "orders")
        hostile = expand_contract(project)
        hostile["modify_modules"][0]["entities"][0]["add_fields"] = [{"name": "note", "type": "str", "max_length": 80, "required": True, "backfill": {"strategy": "constant", "value": "x'); DROP TABLE customers; --"}}]
        hostile["modify_modules"][0]["entities"][0].pop("add_indexes")
        plan = plan_evolution(load_evolution(str(self.write("hostile.json", hostile))))
        self.assertEqual(plan.state, "planned", plan.blockers)
        definitions = plan.files["src/orders_platform/ops/backfills.py"]
        tree = ast.parse(definitions)
        backfills = next(node for node in tree.body if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "BACKFILLS")
        literal = ast.literal_eval(backfills.value)
        self.assertEqual(list(literal.values())[0]["params"]["value"], "x'); DROP TABLE customers; --")
        runner = plan.files["src/orders_platform/ops/backfill.py"]
        self.assertNotIn("DROP TABLE", runner)
        self.assertIn(":value", runner)
        self.assertIn("FOR UPDATE SKIP LOCKED", runner)
        self.assertIn("pg_try_advisory_lock", runner)
        self.assertIn("ON CONFLICT (op_id) DO NOTHING", runner)
        for rel in plan.create:
            if rel.startswith("migrations/versions/"):
                self.assertNotIn("DROP TABLE", plan.files[rel])

    def test_phased_changes_need_an_existing_postgresql_table(self) -> None:
        project = self.copy(INVENTORY, "inventory")
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "catalog", "entities": [{"name": "Product", "add_fields": [{"name": "origin", "type": "str", "required": True, "backfill": {"strategy": "constant", "value": "eu"}}]}]}]}
        plan = plan_evolution(load_evolution(str(self.write("mem.json", doc))))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("backfill_needs_existing_postgresql_table") for b in plan.blockers), plan.blockers)
        doc["modify_modules"][0]["entities"][0] = {"name": "Product", "add_indexes": [{"columns": ["name"]}]}
        plan = plan_evolution(load_evolution(str(self.write("mem2.json", doc))))
        self.assertTrue(any(b.startswith("index_needs_existing_postgresql_table") for b in plan.blockers), plan.blockers)
        orders = self.copy(ORDERS, "orders")
        doc = expand_contract(orders)
        doc["modify_modules"][0]["entities"][0]["add_fields"][0]["backfill"] = {"strategy": "copy_column", "source": "nope"}
        plan = plan_evolution(load_evolution(str(self.write("copy.json", doc))))
        self.assertTrue(any(b.startswith("backfill_source_exists") for b in plan.blockers), plan.blockers)

    def test_applying_the_expand_records_the_declared_state_in_the_manifest(self) -> None:
        project = self.expanded_orders()
        manifest = read_manifest(str(project))
        self.assertEqual(manifest["schema"], MANIFEST_SCHEMA)
        self.assertEqual([c["state_declared"] for c in manifest["phased_changes"]], ["expand", "expand"])
        self.assertEqual([m.get("phase") for m in manifest["migrations"]], ["initial", "expand", "index"])
        self.assertEqual(manifest["concurrent_indexes"][0]["name"], "ix_customers_segment")
        self.assertEqual(manifest["history"][-1]["phased"]["expanded"], [c["op_id"] for c in manifest["phased_changes"]])
        self.assertEqual(set(design.verify_manifest(str(project))["status"].values()), {"identical"})
        model = design.discover(str(project), audit=False)
        self.assertEqual(len(model.alembic["revisions"]), 3)
        self.assertEqual(model.alembic["heads"], [manifest["migrations"][-1]["revision"]])
        self.assertEqual(model.alembic["historical_modified"], [])
        # a second expand with the same declaration is a no-op, not a second operation
        again = run_evolution(str(self.write("again.json", expand_contract(project))), EvolveOptions(apply=False, verify=False))
        self.assertEqual(again["state"], "noop", again["plan"]["blockers"])

    @unittest.skipUnless(shutil.which("ruff") or shutil.which("uvx"), "ruff is not available")
    def test_the_evolved_project_passes_its_own_ruff_configuration(self) -> None:
        project = self.expanded_orders()
        ruff = [shutil.which("ruff")] if shutil.which("ruff") else [shutil.which("uvx"), "ruff"]
        proc = subprocess.run([*ruff, "check", "--no-cache", "."], cwd=str(project), capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-1000:])
        compiled = subprocess.run([PY, "-m", "compileall", "-q", "src", "migrations", "tests"], cwd=str(project), capture_output=True, text=True, timeout=600)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)


class ContractPlanningTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.expanded_orders()
        self.manifest = read_manifest(str(self.project))

    def plan(self, *, evidence: Optional[Dict[str, Any]] = "fresh", authorize: bool = True, **auth) -> Any:
        if evidence == "fresh":
            evidence = evidence_for(self.manifest)
        if evidence is not None:
            (self.tmp / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        elif (self.tmp / "evidence.json").exists():
            (self.tmp / "evidence.json").unlink()
        return plan_evolution(load_evolution(str(self.write("contract.json", self.contract_doc(self.project, self.manifest, **auth)))), authorize_contract=authorize)

    def test_without_explicit_authorization_the_contract_is_blocked(self) -> None:
        plan = self.plan(authorize=False)
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("contract_authorized") for b in plan.blockers), plan.blockers)
        self.assertFalse(any("contract_" in c for c in plan.create))

    def test_without_evidence_the_contract_is_blocked(self) -> None:
        plan = self.plan(evidence=None)
        self.assertTrue(any(b.startswith("contract_evidence_valid") for b in plan.blockers), plan.blockers)
        self.assertTrue(any(b.startswith("contract_evidence_present") for b in plan.blockers), plan.blockers)

    def test_insufficient_evidence_is_refused_for_the_named_reason(self) -> None:
        cases = {
            "stale": (evidence_for(self.manifest, generated_at=datetime.now(timezone.utc) - timedelta(hours=48)), "contract_evidence_fresh"),
            "from the future": (evidence_for(self.manifest, generated_at=datetime.now(timezone.utc) + timedelta(hours=2)), "contract_evidence_fresh"),
            "pending rows": (evidence_for(self.manifest, pending_rows=3), "contract_no_pending_rows"),
            "backfill running": (evidence_for(self.manifest, state="running"), "contract_backfill_complete"),
            "database behind": (evidence_for(self.manifest, current=[self.manifest["migrations"][0]["revision"]]), "contract_database_at_head"),
            "two heads": (evidence_for(self.manifest, current=[self.manifest["migrations"][-1]["revision"], "deadbeef0001"]), "contract_database_at_head"),
            "application version not declared": (evidence_for(self.manifest, application_version=None), "contract_application_version_accredited"),
            "another application version": (evidence_for(self.manifest, application_version="0" * 64), "contract_application_version_accredited"),
            "invalid index": (evidence_for(self.manifest, index_valid=False), "contract_indexes_valid"),
            "missing index": (evidence_for(self.manifest, index_present=False), "contract_indexes_valid"),
            "wrong schema": (dict(evidence_for(self.manifest), schema="other/1.0"), "contract_evidence_valid"),
        }
        for label, (evidence, blocker) in cases.items():
            with self.subTest(case=label):
                plan = self.plan(evidence=evidence)
                self.assertEqual(plan.state, "blocked", label)
                self.assertTrue(any(b.startswith(blocker) for b in plan.blockers), (label, plan.blockers))
        absent = self.plan(evidence=evidence_for(self.manifest, index_present=False))  # an index the database does not hold is missing, not invalid
        detail = next(b for b in absent.blockers if b.startswith("contract_indexes_valid"))
        self.assertIn("invalid: [], missing: ['ix_", detail)
        other = evidence_for(self.manifest)
        other["operations"] = {"0123456789abcdef": next(iter(other["operations"].values()))}
        plan = self.plan(evidence=other)
        self.assertTrue(any(b.startswith("contract_evidence_matches") for b in plan.blockers), plan.blockers)
        (self.tmp / "evidence.json").write_text("{not json", encoding="utf-8")
        plan = plan_evolution(load_evolution(str(self.write("contract.json", self.contract_doc(self.project, self.manifest)))), authorize_contract=True)
        self.assertTrue(any(b.startswith("contract_evidence_valid") for b in plan.blockers), plan.blockers)

    def test_an_unknown_or_already_contracted_operation_is_refused(self) -> None:
        doc = self.contract_doc(self.project, self.manifest)
        doc["modify_modules"][0]["entities"][0]["contract_fields"][0]["operation"] = "0123456789abcdef"
        (self.tmp / "evidence.json").write_text(json.dumps(evidence_for(self.manifest)), encoding="utf-8")
        plan = plan_evolution(load_evolution(str(self.write("unknown.json", doc))), authorize_contract=True)
        self.assertTrue(any(b.startswith("contract_operation_known") for b in plan.blockers), plan.blockers)

    def test_with_every_precondition_the_contract_is_planned_online_and_reversible(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.state, "planned", plan.blockers)
        names = {p.name: p.ok for p in plan.preconditions if p.name.startswith("contract_")}
        for name in ("contract_authorized", "contract_evidence_fresh", "contract_database_at_head", "contract_backfill_complete", "contract_no_pending_rows", "contract_application_version_accredited", "contract_indexes_valid", "contract_reversible"):
            self.assertTrue(names.get(name), (name, names))
        self.assertEqual([p["phase"] for p in plan.migration["phases"]], ["contract"])
        self.assertFalse(plan.migration["phases"][0]["transactional"])
        self.assertTrue(plan.migration["phases"][0]["reversible"])
        contract_file = next(c for c in plan.create if "_contract_" in c)
        source = plan.files[contract_file]
        for fragment in ("_require_complete(", 'CHECK ("{column}" IS NOT NULL) NOT VALID', "VALIDATE CONSTRAINT", "SET NOT NULL", "DROP CONSTRAINT IF EXISTS", "autocommit_block", "DROP NOT NULL", "empty-table-at-contract"):
            self.assertIn(fragment, source, fragment)
        upgrade = source.split("def upgrade")[1].split("def downgrade")[0]
        self.assertIn("_require_complete(", upgrade)
        self.assertIn("_set_not_null_online(", upgrade)
        self.assertNotIn("drop_column", source)
        models = plan.files["src/orders_platform/modules/customers/models.py"]
        self.assertIn("segment: Mapped[str] = mapped_column(String(20), index=True)", models)
        self.assertIn("external_ref: Mapped[UUID | None]", models, "the other phased column stays nullable until its own contract")
        definitions = plan.files["src/orders_platform/ops/backfills.py"]
        literal = ast.literal_eval(next(n for n in ast.parse(definitions).body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "BACKFILLS").value)
        states = {v["column"]: v["state_declared"] for v in literal.values()}
        self.assertEqual(states, {"segment": "contract", "external_ref": "expand"})
        report = run_evolution(str(self.tmp / "contract.json"), EvolveOptions(apply=True, verify=True, authorize_contract=True))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assertTrue(report["contract_authorization"]["authorize_contract"])
        manifest = read_manifest(str(self.project))
        states = {c["column"]: c["state_declared"] for c in manifest["phased_changes"]}
        self.assertEqual(states, {"segment": "contract", "external_ref": "expand"})
        self.assertEqual(manifest["migrations"][-1]["phase"], "contract")
        self.assertEqual(manifest["history"][-1]["phased"]["contracted"], [next(c["op_id"] for c in manifest["phased_changes"] if c["column"] == "segment")])
        again = plan_evolution(load_evolution(str(self.tmp / "contract.json")), authorize_contract=True)
        self.assertTrue(any(b.startswith("contract_operation_pending") for b in again.blockers), again.blockers)
        model = design.discover(str(self.project), audit=False)
        self.assertEqual(len(model.alembic["revisions"]), 4)
        self.assertEqual(model.alembic["historical_modified"], [])

    def test_a_modified_historical_revision_blocks_the_contract(self) -> None:
        first = sorted((self.project / "migrations" / "versions").glob("*.py"))[0]
        first.write_text(first.read_text(encoding="utf-8") + "\n# edited by hand\n", encoding="utf-8")
        plan = self.plan()
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("historical_migrations_unchanged") for b in plan.blockers), plan.blockers)


# --------------------------------------------------------------------------- #
# evidence and the fifth example
# --------------------------------------------------------------------------- #


class EvidenceTests(unittest.TestCase):
    def test_the_loader_accepts_the_shape_and_names_defects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            self.assertEqual(migrations.load_evidence(str(path))[0], None)
            path.write_text("[]", encoding="utf-8")
            self.assertIn("expected a JSON object", migrations.load_evidence(str(path))[1][0])
            path.write_text(json.dumps({"schema": EVIDENCE_SCHEMA, "generated_at": "yesterday", "alembic": {}, "operations": [], "indexes": {}}), encoding="utf-8")
            _doc, errors = migrations.load_evidence(str(path))
            self.assertEqual(len(errors), 4, errors)
            doc, errors = migrations.load_evidence(str(EXAMPLES / "evolutions" / "parcels-migration-evidence.sample.json"))
            self.assertEqual(errors, [])
            self.assertGreater(migrations.evidence_age(doc).total_seconds(), 0)

    def test_the_committed_sample_evidence_is_stale_by_design(self) -> None:
        doc, _ = migrations.load_evidence(str(EXAMPLES / "evolutions" / "parcels-migration-evidence.sample.json"))
        change = {"op_id": "66636e4277641692", "table": "shipments", "column": "carrier", "state_declared": "expand", "application_version": "x"}
        checks = dict((name, ok) for name, ok, _detail in migrations.contract_preconditions(change, doc, head_before="<the index revision id>", authorized=True, declared_by="x", max_age_hours=24.0))
        self.assertFalse(checks["contract_evidence_fresh"])
        self.assertTrue(checks["contract_backfill_complete"])
        self.assertTrue(checks["contract_no_pending_rows"])

    def test_the_fifth_example_generates_and_its_expand_plans(self) -> None:
        spec = design.load_spec(str(PARCELS_SPEC))
        self.assertEqual(spec.persistence["kind"], "postgresql")
        self.assertEqual([m.name for m in spec.modules], ["shipments"])
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "parcels"
            shutil.copytree(PARCELS_SNAPSHOT, project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
            doc = json.loads(EXPAND_EXAMPLE.read_text(encoding="utf-8"))
            doc["project"]["path"] = str(project)
            contract = Path(tmp) / "expand.json"
            contract.write_text(json.dumps(doc), encoding="utf-8")
            plan = plan_evolution(load_evolution(str(contract)))
            self.assertEqual(plan.state, "planned", plan.blockers)
            self.assertEqual([b["operation"] for b in plan.migration["backfills"]], ["66636e4277641692", "5821fb0e87d4b355"])
            self.assertEqual([p["phase"] for p in plan.migration["phases"]], ["expand", "index"])
            contract_doc = json.loads(CONTRACT_EXAMPLE.read_text(encoding="utf-8"))
            self.assertEqual([c["operation"] for c in contract_doc["modify_modules"][0]["entities"][0]["contract_fields"]], ["66636e4277641692", "5821fb0e87d4b355"])


if __name__ == "__main__":
    unittest.main()
