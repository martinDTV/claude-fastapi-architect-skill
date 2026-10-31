"""Increment 4: evolution contract 1.1 (changes to existing modules), additive column migrations in the plan,
selective adoption of a hand-written project, structural transforms through the engine, the recoverable writer
in the apply path, and the adversarial controls that go with them.

Scenario A (generated): the committed inventory-service gains two fields on Product and is verified with its own
tests through the trust model; the PostgreSQL variant (orders-platform) is planned here and executed on a
throwaway cluster by the opt-in module. Scenario B (hand-written): tests/fixtures/adoption20/handmade_shop is
discovered, the items module is adopted, an endpoint and an optional field are added through anchored
transforms, the project's own tests run through the trust model, and the users module never changes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_design import adoption, evolution, journal  # noqa: E402
from fastapi_design.discovery import discover  # noqa: E402
from fastapi_design.generator import MANIFEST  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
ORDERS = EXAMPLES / "generated" / "orders-platform"
HANDMADE = FIXTURES / "adoption20" / "handmade_shop"
ROOT_CLI = REPO_ROOT / "scripts" / "evolve_fastapi.py"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx"))
SCHEMA = evolution.EVOLUTION_SCHEMA


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".fastapi-architect/ops" not in p.as_posix()}


class Inc4Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-inc4-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv"))
        return target

    def write_json(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def modify_contract(self, project: Path, modules: list, name: str = "evolution.json", **extra) -> Path:
        return self.write_json(name, {"schema": SCHEMA, "project": {"path": str(project)}, "modify_modules": modules, **extra})

    def plan(self, contract: Path) -> evolution.EvolutionPlan:
        return evolution.plan_evolution(evolution.load_evolution(str(contract)))

    def evolve(self, contract: Path, **options) -> dict:
        return evolution.run_evolution(str(contract), evolution.EvolveOptions(**options))

    def trusted(self, project: Path) -> dict:
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        return dict(apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: fixture copied by this suite"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=300), test_command=command)

    def adopt_items(self, shop: Path) -> dict:
        contract = self.write_json("adopt.json", {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, "modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}], "notes": "users stays unadopted"})
        return adoption.adopt(adoption.load_adoption_contract(str(contract)))


PRODUCT_FIELDS = [{"name": "catalog", "entities": [{"name": "Product", "add_fields": [{"name": "barcode", "type": "str", "max_length": 64, "required": False}, {"name": "active", "type": "bool", "required": True, "default": True}]}]}]
SHOP_CHANGES = [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/stats", "name": "items_stats", "summary": "Number of items", "response": {"module": "items", "schema_version": 2}}], "add_fields": [{"class": "ItemIn", "name": "sku", "type": "str", "required": False}]}]


# --------------------------------------------------------------------------- #
# contract 1.1
# --------------------------------------------------------------------------- #


class Contract11Tests(unittest.TestCase):
    def test_1_0_contracts_are_still_read_and_1_1_adds_modify_modules(self) -> None:
        old = {"schema": "fastapi-architect/evolution/1.0", "project": {"path": "p"}, "add_modules": [{"name": "x"}]}
        contract, errors = evolution.validate_evolution(old)
        self.assertEqual(errors, [])
        self.assertEqual(contract.schema, "fastapi-architect/evolution/1.0")
        old["modify_modules"] = [{"name": "x", "add_depends_on": ["y"]}]
        _, errors = evolution.validate_evolution(old)
        self.assertTrue(any("needs schema fastapi-architect/evolution/1.1" in e for e in errors))
        new = {"schema": SCHEMA, "project": {"path": "p"}, "modify_modules": [{"name": "x", "entities": [{"name": "X", "add_operations": ["update"]}]}]}
        contract, errors = evolution.validate_evolution(new)
        self.assertEqual(errors, [])
        self.assertEqual(contract.modify_modules[0].entities[0]["add_operations"], ["update"])

    def test_unsupported_and_malformed_changes_are_rejected_with_paths(self) -> None:
        doc = {"schema": SCHEMA, "project": {"path": "p"}, "modify_modules": [
            {"name": "a", "remove_fields": ["x"], "entities": [{"name": "A", "rename_fields": {"x": "y"}}, {"name": "B"}]},
            {"name": "b", "add_endpoints": [{"method": "FETCH", "path": "nope", "name": "Bad"}], "add_fields": [{"class": "lower", "name": "f", "type": "uuid", "required": True}]},
            {"name": "c"},
        ]}
        contract, errors = evolution.validate_evolution(doc)
        self.assertIsNone(contract)
        joined = "\n".join(errors)
        for fragment in ("modify_modules[0].remove_fields: unsupported", "entities[0].rename_fields: unsupported", "entities[1]: nothing to add", "add_endpoints[0].method", "add_endpoints[0].path", "add_endpoints[0].name", "add_endpoints[0].response: required", "add_fields[0].class", "add_fields[0].type", "needs a default", "modify_modules[2]: nothing to change"):
            self.assertIn(fragment, joined, joined)
        _, errors = evolution.validate_evolution({"schema": SCHEMA, "project": {"path": "p"}})
        self.assertTrue(any("nothing to do" in e for e in errors))


# --------------------------------------------------------------------------- #
# scenario A: generated project, changes to an existing module
# --------------------------------------------------------------------------- #


class GeneratedModifyTests(Inc4Case):
    def test_plan_regenerates_only_the_module_files_and_classifies_the_changes(self) -> None:
        project = self.copy(INVENTORY, "inv")
        before = tree(project)
        plan = self.plan(self.modify_contract(project, PRODUCT_FIELDS))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.modified_modules, ["catalog"])
        self.assertEqual(plan.create, [])
        self.assertEqual(sorted(plan.modify), [MANIFEST, "README.md", "src/inventory_service/models/catalog.py", "src/inventory_service/schemas/catalog.py", "src/inventory_service/services/catalog.py", "tests/test_catalog.py"])
        kinds = {(c["kind"], c["classification"]) for c in plan.changes}
        self.assertEqual(kinds, {("add_field", "compatible")})
        self.assertIn("barcode: str | None = Field(default=None, min_length=1, max_length=64)", plan.files["src/inventory_service/schemas/catalog.py"])
        self.assertIn("active: bool = Field(default=True)", plan.files["src/inventory_service/schemas/catalog.py"])
        self.assertIn("active: bool = True", plan.files["src/inventory_service/models/catalog.py"])
        self.assertEqual(plan.public_contracts["routes_added"], [])
        self.assertFalse(plan.migration["required"])
        self.assertEqual(tree(project), before)  # a plan never writes

    def test_required_field_without_default_is_refused_even_in_memory(self) -> None:
        project = self.copy(INVENTORY, "inv")
        plan = self.plan(self.modify_contract(project, [{"name": "catalog", "entities": [{"name": "Product", "add_fields": [{"name": "vendor", "type": "str"}]}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual(plan.changes[0]["classification"], "potentially_incompatible")
        self.assertTrue(any("compatible_request_contract" in b for b in plan.blockers))

    def test_unknown_module_entity_and_cycle_through_dependencies_are_refused(self) -> None:
        project = self.copy(INVENTORY, "inv")
        plan = self.plan(self.modify_contract(project, [{"name": "ghost", "add_depends_on": ["catalog"]}]))
        self.assertTrue(any("module_exists" in b for b in plan.blockers))
        plan = self.plan(self.modify_contract(project, [{"name": "catalog", "entities": [{"name": "Nope", "add_operations": ["get"]}]}], name="e2.json"))
        self.assertTrue(any("entity_exists" in b for b in plan.blockers))
        contract = self.write_json("cycle.json", {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "pricing", "depends_on": ["catalog"], "entities": [{"name": "Price", "fields": [{"name": "amount", "type": "decimal"}]}]}], "modify_modules": [{"name": "catalog", "add_depends_on": ["pricing"]}]})
        plan = self.plan(contract)
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("dependency cycle" in b for b in plan.blockers), plan.blockers)

    def test_adopted_style_changes_are_refused_on_generated_projects(self) -> None:
        project = self.copy(INVENTORY, "inv")
        plan = self.plan(self.modify_contract(project, [{"name": "catalog", "add_endpoints": [{"method": "GET", "path": "/x", "name": "x", "response": {}}]}]))
        self.assertTrue(any("change_supported_for_generated" in b for b in plan.blockers))

    def test_manual_change_on_the_module_blocks_regeneration(self) -> None:
        project = self.copy(INVENTORY, "inv")
        service = project / "src" / "inventory_service" / "services" / "catalog.py"
        service.write_text(service.read_text() + "\n# mine\n")
        plan = self.plan(self.modify_contract(project, PRODUCT_FIELDS))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual([c["kind"] for c in plan.conflicts], ["manual_change_on_integration_point"])
        self.assertEqual([c["path"] for c in plan.conflicts], ["src/inventory_service/services/catalog.py"])

    @unittest.skipUnless(HAS_FASTAPI, "fastapi, httpx and pytest are not importable by this interpreter")
    def test_scenario_a_is_verified_applied_and_idempotent(self) -> None:
        project = self.copy(INVENTORY, "inv")
        before = tree(project)
        options = self.trusted(project)
        contract = self.modify_contract(project, PRODUCT_FIELDS)
        report = self.evolve(contract, **options)
        self.assertEqual(report["state"], "verified", report["verification"]["reasons"])
        run = report["verification"]["tests"]["run"]
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertGreaterEqual(run["counts"]["passed"], 9)
        self.assertEqual(report["verification"]["routes"]["missing"], [])
        self.assertEqual(report["verification"]["routes"]["before"], report["verification"]["routes"]["after"])  # same contract, richer schema
        self.assertEqual(report["verification"]["protected_files"]["unexpectedly_changed"], [])
        self.assertEqual(report["applied"]["state"], "applied")
        after = tree(project)
        changed = sorted(k for k in after if after[k] != before.get(k))
        self.assertEqual(changed, sorted(report["plan"]["files"]["modify"]))
        self.assertEqual(journal.pending_operations(project), [])
        model = discover(project, audit=False)
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"})
        self.assertEqual(model.manifest["history"][-1]["modified_modules"], ["catalog"])
        self.assertEqual([f.name for f in model.baseline_spec.modules[0].entities[0].fields][-2:], ["barcode", "active"])
        again = self.evolve(self.modify_contract(project, PRODUCT_FIELDS, name="again.json"), **options)
        self.assertEqual(again["state"], "noop")
        self.assertEqual(tree(project), after)

    def test_interrupted_publication_is_reported_and_recoverable(self) -> None:
        project = self.copy(INVENTORY, "inv")
        before = tree(project)
        faults = journal.FaultInjector(points=["after_publish:2"])
        report = self.evolve(self.modify_contract(project, PRODUCT_FIELDS), apply=True, faults=faults)
        self.assertEqual(report["state"], "interrupted")
        self.assertEqual(report["applied"]["state"], "interrupted")
        pending = report["applied"]["pending_operations"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0]["published"]), 2)
        # the project is mixed: a new plan refuses until recovery
        blocked = self.plan(self.modify_contract(project, PRODUCT_FIELDS, name="e2.json"))
        self.assertEqual(blocked.state, "blocked")
        self.assertTrue(any("no_pending_operation" in b for b in blocked.blockers))
        op = journal.pending_operations(project)[0]
        rollback = journal.recover(project, op, strategy="rollback")
        self.assertEqual(rollback.state, "rolled_back")
        self.assertEqual(tree(project), before)
        # and the same evolution can then be applied completely
        report = self.evolve(self.modify_contract(project, PRODUCT_FIELDS, name="e3.json"), apply=True)
        self.assertEqual(report["applied"]["state"], "applied")
        self.assertEqual(journal.pending_operations(project), [])

    def test_concurrent_change_between_plan_and_apply_writes_nothing(self) -> None:
        project = self.copy(INVENTORY, "inv")
        contract = self.modify_contract(project, PRODUCT_FIELDS)
        plan = self.plan(contract)
        before = tree(project)
        (project / "README.md").write_text("edited while the operator read the plan\n")
        with self.assertRaises(journal.WriteError):
            journal.write_recoverable(project, plan.files, plan.expected, kind="evolve", description="race")
        after = tree(project)
        self.assertEqual({k: v for k, v in after.items() if k != "README.md"}, {k: v for k, v in before.items() if k != "README.md"})
        self.assertEqual(journal.pending_operations(project), [])


class PostgresColumnPlanTests(Inc4Case):
    CHANGES = [{"name": "customers", "entities": [{"name": "Customer", "add_fields": [{"name": "phone", "type": "str", "max_length": 30, "required": False}, {"name": "tier", "type": "int", "required": True, "default": 1, "min_value": 0}]}]}, {"name": "orders", "entities": [{"name": "Order", "add_operations": ["update"]}]}]

    def test_additive_column_migration_is_planned_after_the_head(self) -> None:
        project = self.copy(ORDERS, "orders")
        head = json.loads((project / MANIFEST).read_text())["migrations"][0]["revision"]
        plan = self.plan(self.modify_contract(project, self.CHANGES))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertTrue(plan.migration["required"])
        self.assertEqual(plan.migration["head_before"], head)
        self.assertEqual(plan.migration["tables"], [])
        self.assertEqual(plan.migration["columns"], ["customers.phone (nullable)", "customers.tier (NOT NULL, server default)"])
        migration = plan.files[plan.migration["file"]]
        self.assertIn(f'down_revision = "{head}"', migration)
        self.assertIn('op.add_column("customers", sa.Column("phone", sa.String(length=30), nullable=True))', migration)
        self.assertIn('op.add_column("customers", sa.Column("tier", sa.Integer(), server_default=sa.text("1"), nullable=False))', migration)
        self.assertIn('op.drop_column("customers", "tier")', migration.split("def downgrade")[1])
        self.assertNotIn("alter_column", migration)
        self.assertNotIn("drop_table", migration)
        self.assertNotIn(f"migrations/versions/{head}_initial_schema.py", plan.files)
        self.assertIn('server_default=text("1")', plan.files["src/orders_platform/modules/customers/models.py"])
        self.assertIn("@router.patch", plan.files["src/orders_platform/modules/orders/router.py"])
        self.assertIn("test_update_order", plan.files["tests/test_orders.py"])
        self.assertIn(f'REVISION = "{plan.migration["new_revision"]}"', plan.files["tests/test_migrations.py"])
        self.assertEqual([r["method"] for r in plan.public_contracts["routes_added"]], ["PATCH"])
        self.assertTrue(any("downgrade drops the column" in r for r in plan.risks))

    def test_not_null_without_backfill_and_not_null_reference_are_refused(self) -> None:
        project = self.copy(ORDERS, "orders")
        plan = self.plan(self.modify_contract(project, [{"name": "customers", "entities": [{"name": "Customer", "add_fields": [{"name": "vat", "type": "str"}]}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual(plan.changes[0]["classification"], "potentially_incompatible")
        self.assertTrue(any("backfill_strategy" in b for b in plan.blockers))
        plan = self.plan(self.modify_contract(project, [{"name": "billing", "entities": [{"name": "Invoice", "add_fields": [{"name": "customer_id", "type": "uuid", "references": "customers.Customer"}]}]}], name="e2.json"))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("NOT NULL reference" in b for b in plan.blockers))
        plan = self.plan(self.modify_contract(project, [{"name": "billing", "add_depends_on": ["customers"], "entities": [{"name": "Invoice", "add_fields": [{"name": "customer_id", "type": "uuid", "references": "customers.Customer", "required": False}]}]}], name="e3.json"))
        self.assertEqual(plan.state, "planned", plan.blockers)
        migration = plan.files[plan.migration["file"]]
        self.assertIn("op.create_foreign_key", migration)
        self.assertIn('op.add_column("invoices", sa.Column("customer_id", sa.Uuid(), nullable=True))', migration)

    def test_schema_drift_in_the_historical_chain_is_refused(self) -> None:
        project = self.copy(ORDERS, "orders")
        head = json.loads((project / MANIFEST).read_text())["migrations"][0]["revision"]
        (project / "migrations" / "versions" / f"{head}_initial_schema.py").write_text("# rewritten\nrevision = '%s'\ndown_revision = None\n" % head)
        plan = self.plan(self.modify_contract(project, self.CHANGES))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("historical_migrations_unchanged" in b for b in plan.blockers))


# --------------------------------------------------------------------------- #
# scenario B: adoption of a hand-written project
# --------------------------------------------------------------------------- #


class AdoptionTests(Inc4Case):
    def test_proposal_lists_candidates_and_adopts_nothing(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        before = tree(shop)
        proposal = adoption.propose_adoption(discover(shop))
        self.assertEqual([c["name"] for c in proposal["candidates"]], ["items", "users"])
        items = proposal["candidates"][0]
        self.assertEqual(items["router"], {"file": "app/routers/items.py", "name": "router", "prefix": "", "effective_prefixes": ["/api/items"], "resolution": "resolved"})
        self.assertEqual(items["related_files"], ["app/schemas.py"])
        self.assertEqual(len(items["routes"]), 3)
        self.assertIn("app/storage.py", proposal["unassigned_files"])
        self.assertEqual(tree(shop), before)

    def test_adopt_writes_only_the_baseline_and_refuses_bad_contracts(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        before = tree(shop)
        report = self.adopt_items(shop)
        self.assertEqual(report["written"], [adoption.ADOPTION])
        self.assertEqual(sorted(report["adopted"]), ["items"])
        self.assertEqual(report["protected_files"], 7)
        after = tree(shop)
        self.assertEqual({k: v for k, v in after.items() if k != adoption.ADOPTION}, before)
        model = discover(shop, audit=False)
        self.assertEqual(model.ownership_of("app/routers/items.py"), "adopted_unchanged")
        self.assertEqual(model.ownership_of("app/routers/users.py"), "unadopted")
        self.assertEqual(sorted(model.to_dict()["adoption"]["modules"]), ["items"])
        bad = [
            ({"modules": [{"name": "items", "router": {"file": "app/routers/items.py"}, "files": ["app/routers/items.py", "app/missing.py"]}]}, "does not exist"),
            ({"modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "nope"}, "files": ["app/routers/items.py"]}]}, "bound 0 times"),
            ({"modules": [{"name": "items", "router": {"file": "app/routers/items.py"}, "files": ["app/routers/items.py"]}, {"name": "dup", "router": {"file": "app/routers/users.py"}, "files": ["app/routers/users.py", "app/routers/items.py"]}]}, "belongs to one module"),
            ({"modules": [{"name": "main", "router": {"file": "app/main.py", "name": "app"}, "files": ["app/main.py"]}]}, "not an APIRouter"),
        ]
        for doc, fragment in bad:
            doc = {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, **doc}
            contract, errors = adoption.validate_adoption(doc)
            if contract is None:
                self.assertTrue(any(fragment in e for e in errors), (fragment, errors))
                continue
            with self.assertRaises(adoption.AdoptionError) as ctx:
                adoption.adopt(contract)
            self.assertTrue(any(fragment in e for e in ctx.exception.errors), (fragment, ctx.exception.errors))
        self.assertEqual({k: v for k, v in tree(shop).items() if k != adoption.ADOPTION}, before)

    def test_generated_projects_and_symlinked_files_are_not_adopted(self) -> None:
        project = self.copy(INVENTORY, "inv")
        doc = {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(project)}, "modules": [{"name": "catalog", "router": {"file": "src/inventory_service/api/catalog.py"}, "files": ["src/inventory_service/api/catalog.py"]}]}
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.adopt(adoption.validate_adoption(doc)[0])
        self.assertTrue(any("generation manifest" in e for e in ctx.exception.errors))
        shop = self.copy(HANDMADE, "shop")
        outside = self.tmp / "outside.py"
        outside.write_text("router = 1\n")
        os.symlink(outside, shop / "app" / "linked.py")
        doc = {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, "modules": [{"name": "items", "router": {"file": "app/routers/items.py"}, "files": ["app/routers/items.py", "app/linked.py"]}]}
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.adopt(adoption.validate_adoption(doc)[0])
        self.assertTrue(any("symlink" in e for e in ctx.exception.errors))

    def test_evolution_refuses_unadopted_modules_manual_changes_and_duplicates(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        self.adopt_items(shop)
        plan = self.plan(self.modify_contract(shop, [{"name": "users", "add_endpoints": [{"method": "GET", "path": "/stats", "name": "users_stats", "response": {}}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("module_is_adopted" in b for b in plan.blockers))
        self.assertEqual(sha(shop / "app" / "routers" / "users.py"), sha(HANDMADE / "app" / "routers" / "users.py"))
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/{item_id}", "name": "get_item_again", "response": {}}]}], name="dup-route.json"))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("duplicate endpoint" in b for b in plan.blockers), plan.blockers)
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/count", "name": "list_items", "response": {}}]}], name="dup-symbol.json"))
        self.assertTrue(any("duplicate symbol" in b for b in plan.blockers), plan.blockers)
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_fields": [{"class": "UserIn", "name": "x", "type": "str"}]}], name="class.json"))
        self.assertEqual(plan.state, "planned", plan.blockers)  # UserIn lives in the adopted app/schemas.py: allowed
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_fields": [{"class": "Missing", "name": "x", "type": "str"}]}], name="missing-class.json"))
        self.assertTrue(any("field_target_found" in b for b in plan.blockers))
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_fields": [{"class": "ItemIn", "name": "price", "type": "float"}]}], name="dup-field.json"))
        self.assertTrue(any("duplicate field" in b for b in plan.blockers), plan.blockers)
        items = shop / "app" / "routers" / "items.py"
        items.write_text(items.read_text() + "\n# human\n")
        plan = self.plan(self.modify_contract(shop, SHOP_CHANGES, name="after-edit.json"))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("adopted_files_unchanged_since_adoption" in b for b in plan.blockers))
        plan = self.plan(self.modify_contract(shop, [], name="addmod.json", add_modules=[{"name": "x", "entities": [{"name": "X", "fields": [{"name": "f", "type": "str"}]}]}]))
        self.assertTrue(any("change_supported_for_adopted" in b for b in plan.blockers))

    def test_ambiguous_class_across_adopted_files_is_refused(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        (shop / "app" / "extra.py").write_text("from pydantic import BaseModel\n\n\nclass ItemIn(BaseModel):\n    other: int = 0\n")
        contract = self.write_json("adopt.json", {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, "modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py", "app/extra.py"]}]})
        adoption.adopt(adoption.load_adoption_contract(str(contract)))
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_fields": [{"class": "ItemIn", "name": "sku", "type": "str"}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("field_target_unambiguous" in b for b in plan.blockers), plan.blockers)

    def test_scenario_b_plan_preview_transforms_and_audit(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        self.adopt_items(shop)
        before = tree(shop)
        report = self.evolve(self.modify_contract(shop, SHOP_CHANGES), apply=False)
        self.assertEqual(report["state"], "audit_clean", report["verification"]["reasons"])
        plan = report["plan"]
        self.assertEqual(plan["kind"], "adopted")
        self.assertEqual(sorted(plan["files"]["modify"]), [adoption.ADOPTION, "app/routers/items.py", "app/schemas.py"])
        self.assertEqual([t["inserted"] for t in plan["transforms"]], [["items_stats"], ["ItemIn.sku"]])
        self.assertTrue(all(t["evidence"]["steps"][0].get("existing_nodes_unchanged") for t in plan["transforms"]))
        self.assertEqual(report["verification"]["routes"]["added"], ["GET /stats (items_stats)"])
        self.assertEqual(report["verification"]["routes"]["missing"], [])
        self.assertEqual(report["verification"]["protected_files"]["unexpectedly_changed"], [])
        self.assertEqual(tree(shop), before)

    @unittest.skipUnless(HAS_FASTAPI, "fastapi, httpx and pytest are not importable by this interpreter")
    def test_scenario_b_is_verified_with_the_projects_own_tests_and_preserves_everything_else(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        self.adopt_items(shop)
        before = tree(shop)
        report = self.evolve(self.modify_contract(shop, SHOP_CHANGES), **self.trusted(shop))
        self.assertEqual(report["state"], "verified", report["verification"]["reasons"])
        self.assertEqual(report["verification"]["tests"]["run"]["counts"]["failed"], 0)
        self.assertEqual(report["applied"]["state"], "applied")
        after = tree(shop)
        changed = sorted(k for k in after if after[k] != before.get(k))
        self.assertEqual(changed, [adoption.ADOPTION, "app/routers/items.py", "app/schemas.py"])
        items = (shop / "app" / "routers" / "items.py").read_text()
        self.assertTrue(items.startswith((HANDMADE / "app" / "routers" / "items.py").read_text().rstrip("\n")))
        self.assertIn("# explicit 404", items)
        self.assertIn('@router.get("/stats", summary="Number of items")', items)
        self.assertIn("    sku: str | None = None", (shop / "app" / "schemas.py").read_text())
        model = discover(shop, audit=False)
        self.assertEqual(model.ownership_of("app/routers/items.py"), "adopted_unchanged")  # the baseline followed the change
        self.assertEqual(model.ownership_of("app/routers/users.py"), "unadopted")
        self.assertEqual(model.adoption["history"][-1]["operation"], "evolve")
        again = self.evolve(self.modify_contract(shop, SHOP_CHANGES, name="again.json"), **self.trusted(shop))
        self.assertEqual(again["state"], "blocked")  # the same endpoint again is a duplicate, not a silent no-op
        self.assertTrue(any("duplicate" in b for b in again["plan"]["blockers"]))
        # a concurrent manual edit after the plan is computed blocks the write
        plan = self.plan(self.modify_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/count", "name": "items_count", "response": {"count": 0}}]}], name="count.json"))
        self.assertEqual(plan.state, "planned", plan.blockers)
        (shop / "app" / "routers" / "items.py").write_text(items + "\n# concurrent\n")
        with self.assertRaises(journal.WriteError):
            journal.write_recoverable(shop, plan.files, plan.expected, kind="evolve", description="race")
        self.assertEqual(sha(shop / "app" / "routers" / "users.py"), before["app/routers/users.py"])

    def test_failing_project_tests_reject_the_change(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        self.adopt_items(shop)
        (shop / "tests" / "test_failing.py").write_text("def test_always_fails():\n    assert False\n")
        before = tree(shop)
        report = self.evolve(self.modify_contract(shop, SHOP_CHANGES), **self.trusted(shop))
        self.assertIn(report["state"], ("rejected", "inconclusive"))
        self.assertNotEqual(report["applied"]["state"], "applied")
        self.assertEqual(tree(shop), before)


class CliInc4Tests(Inc4Case):
    def run_cli(self, *args: str):
        proc = subprocess.run([sys.executable, str(ROOT_CLI), *args], capture_output=True, text=True, cwd=str(self.tmp), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return proc.returncode, proc.stdout, proc.stderr

    def test_propose_adopt_plan_apply_and_recover(self) -> None:
        shop = self.copy(HANDMADE, "shop")
        code, out, err = self.run_cli("propose", str(shop))
        self.assertEqual(code, 0, err)
        self.assertIn("module items:", out)
        self.assertIn("unassigned python files", out)
        contract = self.write_json("adopt.json", {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, "modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}]})
        code, out, err = self.run_cli("adopt", str(contract))
        self.assertEqual(code, 0, err)
        self.assertIn("adopted items", out)
        code, out, _ = self.run_cli("adopt", str(self.write_json("bad.json", {"schema": adoption.ADOPTION_SCHEMA, "project": {"path": str(shop)}, "modules": [{"name": "x", "router": {"file": "nope.py"}, "files": ["nope.py"]}]})))
        self.assertEqual(code, 2)
        evo = self.modify_contract(shop, SHOP_CHANGES)
        code, out, err = self.run_cli("plan", str(evo))
        self.assertEqual(code, 0, err)
        self.assertIn("transform app/routers/items.py", out)
        code, out, err = self.run_cli("apply", str(evo))
        self.assertEqual(code, 0, err)
        self.assertIn("applied: applied (3 files", out)
        code, out, _ = self.run_cli("recover", str(shop))
        self.assertEqual(code, 0)
        self.assertIn("no pending operation", out)
        # an interrupted operation left by a crash is recovered by the CLI
        project = self.copy(INVENTORY, "inv")
        with self.assertRaises(journal.InjectedFailure):
            journal.write_recoverable(project, {"README.md": "x\n", "NEW.md": "y\n"}, {"README.md": sha(project / "README.md"), "NEW.md": None}, kind="test", description="crash", faults=journal.FaultInjector(points=["after_publish:1"]))
        code, out, err = self.run_cli("plan", str(self.modify_contract(project, PRODUCT_FIELDS, name="inv.json")))
        self.assertEqual(code, 1)
        self.assertIn("no_pending_operation", out)
        code, out, err = self.run_cli("recover", str(project))
        self.assertEqual(code, 0, err)
        self.assertIn("rolled_back", out)
        self.assertEqual(sha(project / "README.md"), sha(INVENTORY / "README.md"))
        self.assertFalse((project / "NEW.md").exists())


if __name__ == "__main__":
    unittest.main()
