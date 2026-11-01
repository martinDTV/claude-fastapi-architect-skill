"""v2.2 increment 1: safe column renames, expand and transition (no database here).

The contract 1.3 (``rename_fields``) and its refusals, the plan (a phased ``mirror_column`` operation with a sync
trigger), the transitional code the blueprint generates (model with both columns, request aliases, read from the
old column, dual write in the service, the sync trigger in the expand revision, the runner's predicate, the generated
tests), the manifest 1.4 record, the no-op on repetition, the refusal of retire/contract, and the example files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design.blueprint.phased import KIND_RENAME, MIRROR_STRATEGY, rename_sync_ddl, sync_names  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution, validate_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402
from fastapi_design.migrations import validate_rename_fields  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
TENANT_PROJECTS = EXAMPLES / "generated" / "tenant-projects"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
RENAME_EXAMPLE = EXAMPLES / "evolutions" / "parcels-rename-label.json"


def rename_doc(project: Path, *renames: dict, module: str = "shipments", entity: str = "Shipment", schema: str = EVOLUTION_SCHEMA) -> dict:
    return {"schema": schema, "project": {"path": str(project)}, "modify_modules": [{"name": module, "entities": [{"name": entity, "rename_fields": list(renames) or [{"from": "reference", "to": "label", "strategy": "expand_contract"}]}]}]}


class ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-rename-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path, name: str = "project") -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target

    def write(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def plan(self, doc: dict, name: str = "rename.json"):
        return plan_evolution(load_evolution(str(self.write(name, doc))))

    def errors(self, doc: dict) -> list:
        _contract, errors = validate_evolution(doc, source=None)
        return errors


class ContractValidationTests(ProjectCase):
    def test_rename_fields_needs_schema_13_and_is_refused_under_older_schemas(self) -> None:
        for schema in ("fastapi-architect/evolution/1.1", "fastapi-architect/evolution/1.2"):
            errors = self.errors(rename_doc(PARCELS, schema=schema))
            self.assertTrue(any("rename_fields: needs schema fastapi-architect/evolution/1.3" in e for e in errors), (schema, errors))
        self.assertEqual(self.errors(rename_doc(PARCELS)), [])

    def test_in_place_other_strategies_other_sync_and_malformed_entries_are_refused_by_name(self) -> None:
        cases = {
            "in_place": ({"from": "reference", "to": "label", "strategy": "in_place"}, "not available"),
            "unknown strategy": ({"from": "reference", "to": "label", "strategy": "swap"}, "expected one of expand_contract"),
            "unknown sync": ({"from": "reference", "to": "label", "sync": "application"}, "expected 'trigger'"),
            "same name": ({"from": "reference", "to": "reference"}, "from and to are the same"),
            "bad identifier": ({"from": "Reference", "to": "label"}, ".from: expected"),
            "protected target": ({"from": "reference", "to": "id"}, "is a column every table has"),
            "unknown key": ({"from": "reference", "to": "label", "sql": "x"}, "unknown property"),
        }
        for label, (item, fragment) in cases.items():
            with self.subTest(case=label):
                errors = self.errors(rename_doc(PARCELS, item))
                self.assertTrue(any(fragment in e for e in errors), (label, errors))
        _items, errors = validate_rename_fields([{"from": "a", "to": "b"}, {"from": "b", "to": "c"}], "x")
        self.assertTrue(any("already takes part in another rename" in e for e in errors), errors)
        _items, errors = validate_rename_fields([], "x")
        self.assertTrue(errors)

    def test_the_example_validates_and_the_older_examples_still_mean_the_same(self) -> None:
        contract = load_evolution(str(RENAME_EXAMPLE))
        self.assertEqual(contract.schema, "fastapi-architect/evolution/1.3")
        renames = contract.modify_modules[0].entities[0]["rename_fields"]
        self.assertEqual([(r["from"], r["to"], r["strategy"], r["sync"]) for r in renames], [("reference", "label", "expand_contract", "trigger"), ("weight_kg", "weight", "expand_contract", "trigger")])
        for name in ("parcels-expand-carrier.json", "inventory-add-product-fields.json", "inventory-add-suppliers.json"):
            self.assertEqual(self.errors(json.loads((EXAMPLES / "evolutions" / name).read_text(encoding="utf-8"))), [], name)


class PlanningTests(ProjectCase):
    def test_a_rename_is_planned_as_a_phased_mirror_operation_with_a_sync_trigger(self) -> None:
        project = self.copy(PARCELS)
        plan = self.plan(rename_doc(project))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual([(c["kind"], c["classification"]) for c in plan.changes], [("rename_field", "compatible")])
        op = plan.phased_new[0]
        trigger, function = sync_names("shipments", op["op_id"])
        self.assertEqual((op["kind"], op["table"], op["column"], op["from_column"], op["strategy"], op["params"], op["state_declared"], op["source_of_truth"], op["read_policy"], op["write_policy"]), (KIND_RENAME, "shipments", "label", "reference", MIRROR_STRATEGY, {"source": "reference"}, "expand", "from_column", "from_column", "both"))
        self.assertEqual(op["sync"], {"mechanism": "trigger", "trigger": trigger, "function": function})
        self.assertEqual(op["from_field"]["max_length"], 40)
        phases = [p["phase"] for p in plan.migration["phases"]]
        self.assertEqual(phases, ["expand", "transition"])
        transition = plan.migration["phases"][1]
        self.assertIn("old column", transition["source_of_truth"])
        self.assertIn("arrives with the retire evolution", transition["coexistence"])
        self.assertEqual(plan.migration["renames"][0]["to"], "label")
        self.assertIn("tests/test_backfill.py", plan.tests_required)
        self.assertTrue(any("nothing is retired or dropped" in r for r in plan.risks))
        self.assertEqual(plan.public_contracts["routes_removed"], [])
        self.assertIn("evolve (generated) modify shipments; rename shipments.reference->label", plan.operation_description)

    def test_refusals_name_their_reason(self) -> None:
        project = self.copy(PARCELS)
        cases = {
            "unknown source": ({"from": "nope", "to": "label"}, "rename_source_exists"),
            "target taken": ({"from": "reference", "to": "weight_kg"}, "rename_target_free"),
            "identity column": ({"from": "id", "to": "code"}, "rename_protected_column"),
            "timestamp": ({"from": "created_at", "to": "made_at"}, "rename_protected_column"),
        }
        for label, (item, blocker) in cases.items():
            with self.subTest(case=label):
                plan = self.plan(rename_doc(project, item), name=f"{label}.json")
                self.assertEqual(plan.state, "blocked", label)
                self.assertTrue(any(b.startswith(blocker) for b in plan.blockers), (label, plan.blockers))
        memory = self.copy(INVENTORY, "inventory")
        plan = self.plan(rename_doc(memory, {"from": "sku", "to": "code"}, module="catalog", entity="Product"), name="memory.json")
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("rename_needs_existing_postgresql_table") for b in plan.blockers), plan.blockers)

    def test_foreign_keys_and_tenant_columns_are_never_renamed(self) -> None:
        project = self.copy(TENANT_PROJECTS, "tenant")
        fk = self.plan(rename_doc(project, {"from": "project_id", "to": "parent_id"}, module="tasks", entity="Task"), name="fk.json")
        self.assertTrue(any(b.startswith("rename_protected_column") and "foreign key" in b for b in fk.blockers), fk.blockers)
        tenant = self.plan(rename_doc(project, {"from": "tenant_id", "to": "org_id"}, module="projects", entity="Project"), name="tenant.json")
        self.assertTrue(any(b.startswith("rename_source_exists") or b.startswith("rename_protected_column") for b in tenant.blockers), tenant.blockers)

    def test_applied_then_repeated_is_a_noop_and_a_second_rename_of_the_same_column_is_refused(self) -> None:
        project = self.copy(PARCELS)
        report = run_evolution(str(self.write("rename.json", rename_doc(project))), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assertEqual(report["schema"], "evolution_report/1.7")
        manifest = read_manifest(str(project))
        self.assertEqual(manifest["schema"], "fastapi-architect/generation/1.6")
        entry = manifest["phased_changes"][0]
        self.assertEqual((entry["kind"], entry["from_column"], entry["column"], entry["state_declared"]), ("rename", "reference", "label", "expand"))
        self.assertEqual([m["phase"] for m in manifest["migrations"]], ["initial", "expand"])
        self.assertEqual(manifest["history"][-1]["phased"]["renamed"], [entry["op_id"]])
        again = run_evolution(str(self.write("again.json", rename_doc(project))), EvolveOptions(apply=True, verify=False))
        self.assertEqual(again["state"], "noop")
        chained = self.plan(rename_doc(project, {"from": "label", "to": "tag"}), name="chained.json")
        self.assertTrue(any(b.startswith("rename_column_in_transition") for b in chained.blockers), chained.blockers)
        back = self.plan(rename_doc(project, {"from": "reference", "to": "tag"}), name="back.json")
        self.assertTrue(any(b.startswith("rename_column_in_transition") for b in back.blockers), back.blockers)

    def test_contract_before_retire_is_refused_and_removals_stay_unsupported(self) -> None:
        project = self.copy(PARCELS)
        run_evolution(str(self.write("rename.json", rename_doc(project))), EvolveOptions(apply=True, verify=False))
        op_id = read_manifest(str(project))["phased_changes"][0]["op_id"]
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "contract_fields": [{"name": "label", "operation": op_id}]}]}], "authorization": {"contract": {"declared_by": "tests"}}}
        plan = plan_evolution(load_evolution(str(self.write("contract.json", doc))), authorize_contract=True)
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("contract_rename_retire_first") for b in plan.blockers), plan.blockers)
        removal = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "remove_fields": ["reference"]}]}]}
        self.assertTrue(any("remove_fields" in e and "unsupported" in e for e in self.errors(removal)))

class GeneratedCodeTests(ProjectCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._root = tempfile.TemporaryDirectory(prefix="fa-rename-gen-")
        cls.project = Path(cls._root.name).resolve() / "parcels"
        shutil.copytree(PARCELS, cls.project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        doc = json.loads(RENAME_EXAMPLE.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(cls.project)
        contract = Path(cls._root.name) / "rename.json"
        contract.write_text(json.dumps(doc), encoding="utf-8")
        cls.report = run_evolution(str(contract), EvolveOptions(apply=True, verify=False))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._root.cleanup()

    def read(self, rel: str) -> str:
        return (self.project / rel).read_text(encoding="utf-8")

    def test_the_model_keeps_both_columns_and_the_new_one_is_nullable(self) -> None:
        self.assertEqual(self.report["applied"]["state"], "applied", self.report["plan"]["blockers"])
        model = self.read("src/parcels/models/shipments.py")
        self.assertIn("label: Mapped[str | None] = mapped_column(String(40), nullable=True)", model)
        self.assertIn("reference: Mapped[str] = mapped_column(String(40))  # transition: source of truth", model)
        self.assertIn("weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)  # transition: source of truth", model)

    def test_request_schemas_accept_both_names_and_the_read_schema_reads_the_old_column(self) -> None:
        schemas = self.read("src/parcels/schemas/shipments.py")
        self.assertIn('validation_alias=AliasChoices("label", "reference")', schemas)
        self.assertIn('validation_alias=AliasChoices("weight", "weight_kg")', schemas)
        read = schemas[schemas.index("class ShipmentRead"):]
        self.assertIn('validation_alias=AliasChoices("reference", "label")', read)
        self.assertIn('reference: str = Field(json_schema_extra={"deprecated": True})', read)

    def test_the_service_writes_both_columns_on_create_and_update(self) -> None:
        service = self.read("src/parcels/services/shipments.py")
        self.assertIn("_TRANSITIONS_SHIPMENT = {'label': 'reference', 'weight': 'weight_kg'}", service)
        self.assertIn("Shipment(label=data.label, weight=data.weight, reference=data.label, weight_kg=data.weight)", service)
        self.assertIn("for new_name, old_name in _TRANSITIONS_SHIPMENT.items():", service)
        self.assertIn("setattr(shipment, old_name, getattr(shipment, new_name))", service)

    def test_the_expand_revision_adds_nullable_columns_and_one_sync_trigger_per_rename(self) -> None:
        revision = next(self.project.glob("migrations/versions/*_expand_*.py")).read_text(encoding="utf-8")
        ops = read_manifest(str(self.project))["phased_changes"]
        for op in ops:
            trigger, function = sync_names("shipments", op["op_id"])
            self.assertIn(f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql", revision)
            self.assertIn(f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON \"shipments\"", revision)
            self.assertIn(f"DROP TRIGGER IF EXISTS {trigger} ON \"shipments\"", revision)
            self.assertIn(f'NEW."{op["from_column"]}" IS DISTINCT FROM OLD."{op["from_column"]}"', revision)
        self.assertIn('op.add_column("shipments", sa.Column("label", sa.String(length=40), nullable=True))', revision)
        self.assertNotIn("RENAME COLUMN", revision)
        self.assertNotIn("drop_column", revision.split("def downgrade")[0])
        up, down = rename_sync_ddl("t", "a", "b", "0123456789abcdef")
        self.assertTrue(any("RETURN NEW;" in line for line in up) and any("DROP FUNCTION IF EXISTS" in line for line in down))

    def test_the_runner_definitions_and_tests_know_the_renames(self) -> None:
        definitions = self.read("src/parcels/ops/backfills.py")
        self.assertIn("'strategy': 'mirror_column'", definitions)
        self.assertIn("'from_column': 'reference'", definitions)
        self.assertIn("'source_of_truth': 'from_column'", definitions)
        runner = self.read("src/parcels/ops/backfill.py")
        self.assertIn("IS DISTINCT FROM", runner)
        self.assertIn('EVIDENCE_SCHEMA = "fastapi-architect/migration-evidence/1.2"', runner)
        self.assertIn("columns_agree", runner)
        tests = self.read("tests/test_backfill.py")
        self.assertIn('PENDING_PREDICATE = "\\"label\\" IS DISTINCT FROM \\"reference\\""', tests)
        self.assertIn("def test_the_sync_trigger_mirrors_writes_of_either_column", tests)
        module_tests = self.read("tests/test_shipments.py")
        self.assertIn("def test_create_shipment_accepts_the_former_name_reference_and_returns_both_names", module_tests)
        self.assertIn("def test_update_shipment_through_the_former_name_reference_writes_both_columns", module_tests)
        self.assertNotIn("former_name_weight_kg", module_tests)  # decimals do not round-trip as JSON literals: no echo test

    def test_the_evolved_project_passes_its_own_ruff_configuration_and_compiles(self) -> None:
        proc = subprocess.run(["uvx", "ruff", "check", "."], cwd=str(self.project), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:])
        for rel in ("src/parcels/models/shipments.py", "src/parcels/schemas/shipments.py", "src/parcels/services/shipments.py", "src/parcels/ops/backfill.py", "tests/test_backfill.py", "tests/test_shipments.py"):
            compile(self.read(rel), rel, "exec")


if __name__ == "__main__":
    unittest.main()
