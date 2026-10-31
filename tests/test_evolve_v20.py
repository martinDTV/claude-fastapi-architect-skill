"""FastAPI Architect v2.0, increment 3: project discovery, evolution contracts, the safe writer and the vertical case.

The fixture is the committed ``inventory-service`` example, copied into a temporary directory for every test that
writes. Evidence over quantity: the vertical case applies a second module with its tests executed through the
trust model (when fastapi is importable), re-applies it (no-op), preserves a manual change on a managed file,
refuses to regenerate a hand-edited integration point, and refuses a project without a manifest. The adversarial
controls cover stale baselines, concurrent changes, unsafe paths, symlinks, unexpected managed content, duplicate
modules and routes, cycles, modified historical migrations, two Alembic heads, write errors, failed verification,
and execution without authorization.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_design import evolution  # noqa: E402
from fastapi_design.discovery import discover  # noqa: E402
from fastapi_design.generator import MANIFEST, WriteError, write_files  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
INVENTORY = EXAMPLES / "generated" / "inventory-service"
ORDERS = EXAMPLES / "generated" / "orders-platform"
ADD_SUPPLIERS = EXAMPLES / "evolutions" / "inventory-add-suppliers.json"
ADD_SHIPMENTS = EXAMPLES / "evolutions" / "orders-add-shipments.json"
ROOT_CLI = REPO_ROOT / "scripts" / "evolve_fastapi.py"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class EvolveCase(unittest.TestCase):
    """A fresh copy of a committed example and a contract pointing at it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-evolve-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy_example(self, example: Path = INVENTORY, name: str = "project") -> Path:
        target = self.tmp / name
        shutil.copytree(example, target, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv"))
        return target

    def contract(self, project_dir: Path, template: Path = ADD_SUPPLIERS, name: str = "evolution.json", **overrides) -> Path:
        doc = _load(template)
        doc["project"]["path"] = str(project_dir)
        for key, value in overrides.items():
            doc[key] = value
        path = self.tmp / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def plan(self, contract: Path, **kwargs) -> evolution.EvolutionPlan:
        return evolution.plan_evolution(evolution.load_evolution(str(contract)), **kwargs)

    def run_evolution(self, contract: Path, **options) -> dict:
        return evolution.run_evolution(str(contract), evolution.EvolveOptions(**options))


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


class DiscoveryTests(unittest.TestCase):
    def test_generated_layered_project(self) -> None:
        model = discover(str(INVENTORY)).to_dict()
        self.assertEqual(model["package"], "inventory_service")
        self.assertEqual(model["source_root"], "src")
        self.assertEqual(model["layout"], "layered")
        self.assertEqual(model["modules"], ["catalog"])
        self.assertEqual(model["generated"]["schema"], "fastapi-architect/generation/1.1")
        self.assertTrue(model["generated"]["has_spec_document"])
        self.assertEqual(model["baseline_contract"]["modules"], ["catalog"])
        self.assertEqual(set(model["ownership"].values()), {"managed_unchanged"})
        routes = {(r["method"], r["effective_paths"][0]) for r in model["routes"]}
        self.assertIn(("POST", "/api/v1/products"), routes)
        self.assertIn(("GET", "/health"), routes)
        self.assertEqual([s["class"] for s in model["settings"]], ["Settings"])
        self.assertEqual([s["class"] for s in model["services"]], ["ProductService"])
        self.assertIn("ProductRepository", [r["class"] for r in model["repositories"]])
        self.assertEqual(model["lifespan"]["function"], "lifespan")
        self.assertIsNone(model["alembic"])
        self.assertEqual(model["uncertainties"], [])

    def test_generated_modular_postgresql_project(self) -> None:
        model = discover(str(ORDERS)).to_dict()
        self.assertEqual(model["layout"], "modular")
        self.assertEqual(model["modules"], ["billing", "customers", "orders"])
        self.assertEqual(sorted(m["table"] for m in model["sqlalchemy_models"]), ["customers", "invoices", "orders"])
        self.assertNotIn("Base", [m["class"] for m in model["sqlalchemy_models"]])
        self.assertEqual(model["alembic"]["heads"], [model["generated"]["migrations"][0]["revision"]])
        self.assertTrue(model["alembic"]["chain_ok"])
        self.assertEqual(model["alembic"]["historical_modified"], [])
        self.assertEqual(model["alembic"]["revisions"][0]["ownership"], "managed_unchanged")
        self.assertIsNotNone(model["declared_contracts"])
        self.assertEqual(model["audit_summary"]["contracts"]["unauthenticated_not_public"], 0)

    def test_hand_written_project_reports_uncertainty_instead_of_guessing(self) -> None:
        model = discover(str(FIXTURES / "regressions_v20" / "nested_routers")).to_dict()
        self.assertIsNone(model["generated"])
        self.assertEqual(set(model["ownership"].values()), {"unknown"})
        self.assertEqual(model["layout"], "unknown")
        self.assertTrue(any("ownership is unknown" in u for u in model["uncertainties"]))
        self.assertTrue(any("layout is not one the generator produces" in u for u in model["uncertainties"]))
        self.assertGreaterEqual(len(model["routes"]), 4)  # routes are still reported: discovery describes, evolution refuses

    def test_discovery_never_executes_the_project(self) -> None:
        source = (SKILL_SCRIPTS / "fastapi_design" / "discovery.py").read_text(encoding="utf-8")
        for forbidden in ("importlib.import_module", "exec(", "eval(", "subprocess", "runpy", "__import__("):
            self.assertNotIn(forbidden, source, forbidden)


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #


class EvolutionContractTests(unittest.TestCase):
    def test_example_contracts_validate(self) -> None:
        for path in (ADD_SUPPLIERS, ADD_SHIPMENTS):
            contract, errors = evolution.validate_evolution(_load(path), str(path))
            self.assertEqual(errors, [], path.name)
            self.assertIn(contract.schema, evolution.EVOLUTION_SCHEMAS)  # the increment 3 examples are 1.0 contracts, still read
            self.assertEqual(len(contract.digest), 64)

    def test_invalid_contracts_are_rejected_with_paths(self) -> None:
        cases = [
            ({"schema": "x"}, ["evolution.schema", "evolution.project", "nothing to do"]),
            ({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": "p", "bogus": 1}, "add_modules": [{"name": "Bad-Name"}], "extra": 1}, ["evolution.project.bogus", "add_modules[0].name", "evolution.extra"]),
            ({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": "p", "expected_spec_digest": "zz"}, "add_modules": [{"name": "ok"}], "invariants": ["made_up"], "files": {"allow_modify": ["../etc/passwd"], "preserve": "README.md"}}, ["expected_spec_digest", "evolution.invariants", "allow_modify", "files.preserve"]),
        ]
        for doc, fragments in cases:
            contract, errors = evolution.validate_evolution(doc)
            self.assertIsNone(contract)
            joined = "\n".join(errors)
            for fragment in fragments:
                self.assertIn(fragment, joined, joined)


# --------------------------------------------------------------------------- #
# plan and safe writer
# --------------------------------------------------------------------------- #


class PlanTests(EvolveCase):
    def test_plan_for_the_committed_example_is_a_preview_that_writes_nothing(self) -> None:
        project = self.copy_example()
        before = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.added_modules, ["suppliers"])
        self.assertEqual(sorted(plan.create), ["src/inventory_service/api/suppliers.py", "src/inventory_service/dependencies/suppliers.py", "src/inventory_service/models/suppliers.py", "src/inventory_service/repositories/suppliers.py", "src/inventory_service/schemas/suppliers.py", "src/inventory_service/services/suppliers.py", "tests/test_suppliers.py"])
        self.assertEqual(sorted(plan.modify), [MANIFEST, "README.md", "src/inventory_service/api/router.py", "src/inventory_service/main.py"])
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(plan.public_contracts["routes_removed"], [])
        self.assertEqual(len(plan.public_contracts["routes_added"]), 5)
        self.assertFalse(plan.migration["required"])
        self.assertIn("suppliers_router", plan.diffs["src/inventory_service/api/router.py"])
        after = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        self.assertEqual(before, after)  # a plan never writes

    def test_style_is_kept_even_when_the_advisor_would_change_it(self) -> None:
        project = self.copy_example()
        modules = [{"name": n, "entities": [{"name": n.capitalize(), "fields": [{"name": "label", "type": "str"}]}]} for n in ("alpha", "beta", "gamma")]
        plan = self.plan(self.contract(project, add_modules=modules))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.style, "layered")
        self.assertIn("modular_monolith", plan.style_warning or "")
        self.assertTrue(all(p.startswith(("src/inventory_service/api/", "src/inventory_service/dependencies/", "src/inventory_service/models/", "src/inventory_service/repositories/", "src/inventory_service/schemas/", "src/inventory_service/services/", "tests/")) for p in plan.create), plan.create)
        self.assertEqual(plan.evolved_spec.architecture["style_preference"], "layered")

    def test_unknown_project_is_refused(self) -> None:
        contract = self.contract(FIXTURES / "regressions_v20" / "nested_routers")
        doc = _load(contract)
        doc["project"].pop("package")
        contract.write_text(json.dumps(doc), encoding="utf-8")
        plan = self.plan(contract)
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("generation_manifest_present" in b for b in plan.blockers))

    def test_stale_baseline_is_refused(self) -> None:
        project = self.copy_example()
        plan = self.plan(self.contract(project, project={"path": str(project), "expected_spec_digest": "0123456789ab"}))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("stale baseline" in b for b in plan.blockers))

    def test_duplicate_module_with_a_different_definition_and_duplicate_routes_are_refused(self) -> None:
        project = self.copy_example()
        plan = self.plan(self.contract(project, add_modules=[{"name": "catalog", "entities": [{"name": "Thing", "fields": [{"name": "x", "type": "int"}]}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("already exists with a different definition" in b for b in plan.blockers))
        plan = self.plan(self.contract(project, add_modules=[{"name": "pricing", "entities": [{"name": "Product", "fields": [{"name": "price", "type": "decimal"}]}]}]))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("routes_are_unique" in b and "/products" in b for b in plan.blockers))

    def test_dependency_cycle_and_bad_reference_are_refused_with_contract_paths(self) -> None:
        project = self.copy_example()
        modules = [
            {"name": "a", "depends_on": ["b"], "entities": [{"name": "A", "fields": [{"name": "x", "type": "int"}]}]},
            {"name": "b", "depends_on": ["a"], "entities": [{"name": "B", "fields": [{"name": "a_id", "type": "uuid", "references": "catalog.Product"}]}]},
        ]
        plan = self.plan(self.contract(project, add_modules=modules))
        self.assertEqual(plan.state, "blocked")
        joined = "\n".join(plan.blockers)
        self.assertIn("dependency cycle", joined)
        self.assertIn("evolution.add_modules[1].entities[0].fields[0].references", joined)  # paths point at the evolution contract

    def test_manual_change_on_an_integration_point_blocks_and_elsewhere_is_preserved(self) -> None:
        project = self.copy_example()
        service = project / "src" / "inventory_service" / "services" / "catalog.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# hand-written\n", encoding="utf-8")
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.preserved_manual, ["src/inventory_service/services/catalog.py"])
        self.assertNotIn("src/inventory_service/services/catalog.py", plan.files)
        router = project / "src" / "inventory_service" / "api" / "router.py"
        router.write_text(router.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual([c["kind"] for c in plan.conflicts], ["manual_change_on_integration_point"])

    def test_managed_file_with_unexpected_content_is_a_manual_change(self) -> None:
        project = self.copy_example()
        main = project / "src" / "inventory_service" / "main.py"
        main.write_text("print('replaced')\n", encoding="utf-8")
        model = discover(str(project), audit=False)
        self.assertEqual(model.ownership_of("src/inventory_service/main.py"), "managed_modified")
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "blocked")
        self.assertIn("src/inventory_service/main.py", [c["path"] for c in plan.conflicts])

    def test_unmanaged_file_in_the_way_and_preserved_file_are_conflicts(self) -> None:
        project = self.copy_example()
        (project / "tests" / "test_suppliers.py").write_text("# mine\n", encoding="utf-8")
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual([c["kind"] for c in plan.conflicts], ["file_in_the_way"])
        project2 = self.copy_example(name="project2")
        plan = self.plan(self.contract(project2, name="e2.json", files={"preserve": ["README.md"]}))
        self.assertEqual(plan.state, "blocked")
        self.assertEqual([c["kind"] for c in plan.conflicts], ["preserved_file_would_change"])

    def test_symlinked_project_is_refused(self) -> None:
        project = self.copy_example()
        link = self.tmp / "link"
        os.symlink(project, link)
        plan = self.plan(self.contract(link))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("symlink" in b for b in plan.blockers))

    def test_writer_refuses_escapes_symlinks_and_concurrent_changes(self) -> None:
        project = self.copy_example()
        for bad in ("../outside.py", "/abs.py", "a/../../b.py"):
            with self.assertRaises(design.GenerationError):
                write_files(project, {bad: "x"}, {bad: None})
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, project / "linked")
        with self.assertRaises(design.GenerationError):
            write_files(project, {"linked/new.py": "x"}, {"linked/new.py": None})
        self.assertFalse((outside / "new.py").exists())
        readme = project / "README.md"
        expected = design.generator.sha256_text(readme.read_text(encoding="utf-8"))
        readme.write_text("changed meanwhile\n", encoding="utf-8")
        with self.assertRaises(WriteError) as ctx:
            write_files(project, {"README.md": "new", "fresh.txt": "x"}, {"README.md": expected, "fresh.txt": None})
        self.assertIn("concurrent change", str(ctx.exception))
        self.assertFalse((project / "fresh.txt").exists())  # nothing written when a precondition fails
        self.assertEqual(readme.read_text(encoding="utf-8"), "changed meanwhile\n")

    def test_write_error_leaves_the_project_untouched(self) -> None:
        project = self.copy_example()
        plan = self.plan(self.contract(project))
        self.assertEqual(plan.state, "planned", plan.blockers)
        locked = project / "src" / "inventory_service" / "services"
        mode = locked.stat().st_mode
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(locked.chmod, mode)
        if os.access(locked, os.W_OK):
            self.skipTest("the directory stays writable for this user (root or permissive filesystem)")
        with self.assertRaises(WriteError):
            write_files(project, plan.files, plan.expected)
        locked.chmod(mode)
        self.assertFalse((project / "tests" / "test_suppliers.py").exists())
        self.assertFalse(list(project.rglob("*.fa-tmp")))
        self.assertEqual(design.verify_manifest(str(project))["status"]["src/inventory_service/api/router.py"], "identical")


# --------------------------------------------------------------------------- #
# vertical case: apply, verify, idempotency
# --------------------------------------------------------------------------- #


class VerticalCaseTests(EvolveCase):
    def test_apply_without_authorization_is_audit_clean_and_runs_nothing(self) -> None:
        project = self.copy_example()
        report = self.run_evolution(self.contract(project), apply=True)
        self.assertEqual(report["state"], "audit_clean", report["verification"]["reasons"])
        self.assertIsNone(report["verification"]["tests"])
        self.assertEqual(report["verification"]["audit"]["blocking_new"], [])
        self.assertEqual(report["verification"]["routes"]["missing"], [])
        self.assertEqual(report["verification"]["protected_files"]["unexpectedly_changed"], [])
        self.assertEqual(report["applied"]["state"], "applied")
        self.assertEqual(len(report["applied"]["written"]), 11)
        model = discover(str(project), audit=False)
        self.assertEqual(model.modules, ["catalog", "suppliers"])
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"})
        self.assertEqual([h["operation"] for h in model.manifest["history"]], ["generate", "evolve"])
        self.assertEqual(model.baseline_spec.modules[-1].name, "suppliers")
        again = self.run_evolution(self.contract(project, name="again.json"), apply=True)
        self.assertEqual(again["state"], "noop")
        self.assertEqual(again["applied"]["state"], "nothing_to_apply")

    def test_test_command_without_authorization_executes_nothing(self) -> None:
        project = self.copy_example()
        report = self.run_evolution(self.contract(project), apply=False, test_command="python3 -m pytest -q")
        tests = report["verification"]["tests"]
        self.assertFalse(tests["ran"])
        self.assertEqual(tests["denied_code"], "execution_unauthorized")
        self.assertEqual(report["state"], "inconclusive")

    def test_untrusted_profile_fails_closed(self) -> None:
        project = self.copy_example()
        report = self.run_evolution(self.contract(project), apply=True, authorization=Authorization(execution=True, trust="untrusted", scope=str(project)), policy=ExecutionPolicy(authorized=True), test_command="python3 -m pytest -q")
        self.assertEqual(report["state"], "inconclusive")
        self.assertEqual(report["applied"]["state"], "refused")
        self.assertFalse((project / "tests" / "test_suppliers.py").exists())

    @unittest.skipUnless(HAS_FASTAPI, "fastapi, httpx and pytest are not importable by this interpreter")
    def test_vertical_case_is_verified_with_tests_through_the_trust_model_and_idempotent(self) -> None:
        project = self.copy_example()
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        options = dict(apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: fixture copied by this suite"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=300), test_command=command)
        report = self.run_evolution(self.contract(project), **options)
        self.assertEqual(report["state"], "verified", report["verification"]["reasons"])
        tests = report["verification"]["tests"]
        run = tests["run"]
        self.assertEqual(run["outcome"], "passed", run.get("stdout_tail"))
        self.assertEqual(run["runner"], "pytest-junit")
        self.assertTrue(run["evidence"]["matched"])
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertGreaterEqual(run["counts"]["passed"], 18)  # 9 catalog tests + 9 supplier tests
        passed = run["evidence"]["tests"]["passed"]
        self.assertTrue(any(name.endswith("test_create_supplier") for name in passed))
        self.assertTrue(any(name.endswith("test_create_product") for name in passed))  # the existing module keeps working
        self.assertEqual(report["applied"]["state"], "applied")
        self.assertEqual(report["verification"]["routes"]["before"], 6)
        self.assertEqual(report["verification"]["routes"]["after"], 11)
        # second run: nothing to do, nothing written
        digests_before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
        again = self.run_evolution(self.contract(project, name="again.json"), **options)
        self.assertEqual(again["state"], "noop")
        self.assertEqual({p: p.read_bytes() for p in project.rglob("*") if p.is_file()}, digests_before)
        # a manual change on a managed, non-integration file survives the next evolution
        service = project / "src" / "inventory_service" / "services" / "catalog.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# hand-written note\n", encoding="utf-8")
        third = self.contract(project, name="returns.json", add_modules=[{"name": "returns", "entities": [{"name": "ReturnRequest", "fields": [{"name": "reason", "type": "str", "max_length": 200}], "operations": ["create", "list"]}]}])
        report3 = self.run_evolution(third, **options)
        self.assertEqual(report3["state"], "verified", report3["verification"]["reasons"])
        self.assertEqual(report3["plan"]["files"]["preserved_manual_changes"], ["src/inventory_service/services/catalog.py"])
        self.assertIn("# hand-written note", service.read_text(encoding="utf-8"))
        self.assertEqual(discover(str(project), audit=False).ownership_of("src/inventory_service/services/catalog.py"), "managed_modified")

    def test_failed_verification_refuses_to_write(self) -> None:
        project = self.copy_example()
        command = f"{sys.executable} -c \"import sys; sys.exit(1)\""  # an allowed interpreter that fails: the runner is generic -> not evidence
        report = self.run_evolution(self.contract(project), apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project)), policy=ExecutionPolicy(authorized=True, command=command), test_command=command)
        self.assertIn(report["state"], ("rejected", "inconclusive"))
        self.assertNotEqual(report["applied"]["state"], "applied")
        self.assertFalse((project / "tests" / "test_suppliers.py").exists())
        self.assertEqual(set(discover(str(project), audit=False).ownership.values()), {"managed_unchanged"})


# --------------------------------------------------------------------------- #
# PostgreSQL plan (no database needed)
# --------------------------------------------------------------------------- #


class PostgresPlanTests(EvolveCase):
    def test_additive_migration_is_planned_with_the_chain_kept(self) -> None:
        project = self.copy_example(ORDERS, "orders")
        plan = self.plan(self.contract(project, template=ADD_SHIPMENTS))
        self.assertEqual(plan.state, "planned", plan.blockers)
        head = _load(project / MANIFEST)["migrations"][0]["revision"]
        self.assertTrue(plan.migration["required"])
        self.assertEqual(plan.migration["head_before"], head)
        self.assertEqual(plan.migration["tables"], ["shipments"])
        self.assertEqual(plan.kept, [f"migrations/versions/{head}_initial_schema.py"])
        migration = plan.files[plan.migration["file"]]
        self.assertIn(f'down_revision = "{head}"', migration)
        self.assertNotIn("drop_table", migration.split("def downgrade")[0])
        self.assertNotIn("alter_column", migration)
        self.assertIn('["tenant_id", "order_id"],', migration)  # composite foreign key to the existing tenant table
        self.assertIn('["orders.tenant_id", "orders.id"],', migration)
        self.assertIn('ondelete="RESTRICT"', migration)
        self.assertIn(f'REVISION = "{plan.migration["new_revision"]}"', plan.files["tests/test_migrations.py"])
        self.assertNotIn(f"migrations/versions/{head}_initial_schema.py", plan.files)  # historical file untouched
        self.assertIn("tests/test_shipments.py", plan.create)

    def test_modified_historical_migration_and_two_heads_are_refused(self) -> None:
        project = self.copy_example(ORDERS, "orders")
        head = _load(project / MANIFEST)["migrations"][0]["revision"]
        historical = project / "migrations" / "versions" / f"{head}_initial_schema.py"
        historical.write_text(historical.read_text(encoding="utf-8") + "\n# tweaked\n", encoding="utf-8")
        plan = self.plan(self.contract(project, template=ADD_SHIPMENTS))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("historical_migrations_unchanged" in b for b in plan.blockers), plan.blockers)
        project2 = self.copy_example(ORDERS, "orders2")
        (project2 / "migrations" / "versions" / "ffffffffffff_other_head.py").write_text(f'revision = "ffffffffffff"\ndown_revision = "{head}"\n\n\ndef upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n', encoding="utf-8")
        (project2 / "migrations" / "versions" / "eeeeeeeeeeee_another_head.py").write_text(f'revision = "eeeeeeeeeeee"\ndown_revision = "{head}"\n\n\ndef upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n', encoding="utf-8")
        model = discover(str(project2), audit=False)
        self.assertEqual(len(model.alembic["heads"]), 2)
        plan = self.plan(self.contract(project2, template=ADD_SHIPMENTS, name="e2.json"))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any("alembic_chain_is_linear" in b for b in plan.blockers), plan.blockers)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class CliTests(EvolveCase):
    def run_cli(self, *args: str):
        proc = subprocess.run([sys.executable, str(ROOT_CLI), *args], capture_output=True, text=True, cwd=str(self.tmp), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return proc.returncode, proc.stdout, proc.stderr

    def test_discover_plan_apply_and_exit_codes(self) -> None:
        project = self.copy_example()
        code, out, err = self.run_cli("discover", str(project))
        self.assertEqual(code, 0, err)
        self.assertIn("layout: layered", out)
        self.assertIn("managed_unchanged 27", out)
        contract = self.contract(project)
        code, out, err = self.run_cli("plan", str(contract), "--format", "json", "--output", str(self.tmp / "plan.json"))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], evolution.REPORT_SCHEMA)
        self.assertEqual(report["state"], "audit_clean")
        self.assertEqual(report["applied"]["state"], "not_applied")
        self.assertFalse((project / "tests" / "test_suppliers.py").exists())
        self.assertEqual(self.run_cli("apply", str(contract), "--test-command", "pytest")[0], 2)  # without --authorize-execution
        code, out, err = self.run_cli("apply", str(contract))
        self.assertEqual(code, 0, err)
        self.assertIn("applied: applied (11 files", out)
        self.assertTrue((project / "tests" / "test_suppliers.py").is_file())
        code, out, _ = self.run_cli("apply", str(contract))
        self.assertEqual(code, 0)
        self.assertIn("state: noop", out)
        code, out, _ = self.run_cli("apply", str(contract), "--strict")
        self.assertEqual(code, 1)  # noop is not verified
        unknown = self.contract(FIXTURES / "regressions_v20" / "nested_routers", name="unknown.json", project={"path": str(FIXTURES / "regressions_v20" / "nested_routers")})
        code, out, _ = self.run_cli("plan", str(unknown))
        self.assertEqual(code, 1)
        self.assertIn("generation_manifest_present", out)
        code, _, err = self.run_cli("plan", str(self.tmp / "missing.json"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
