"""FastAPI Architect v2.0, first increment: contract validation, architecture advice, blueprint, safe generation,
verification through the audit engine and the v1.9 trust model, report contracts and the CLI.

Fixtures: ``tests/fixtures/design20/specs/valid`` (every one validates and generates) and ``invalid`` (every one is
rejected; the ``x-expect`` annotation lists substrings that must appear in the error list, with their JSON paths).
Nothing in a generated project is executed unless the test says so explicitly through an ``Authorization``; the
one test that runs generated tests uses the ``fixture`` trust profile on a project this suite just generated and is
skipped when fastapi or pytest are not importable by the current interpreter.
"""

from __future__ import annotations

import ast
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

import fastapi_design as design  # noqa: E402
from fastapi_audit.model import TOOL_VERSION  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402

SPECS = FIXTURES / "design20" / "specs"
EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
INVENTORY = EXAMPLES / "specs" / "inventory-service.json"
ORDERS = EXAMPLES / "specs" / "orders-platform.json"
SNAPSHOT = EXAMPLES / "generated" / "inventory-service"
ROOT_CLI = REPO_ROOT / "scripts" / "design_fastapi.py"
SKILL_CLI = SKILL_SCRIPTS / "design_fastapi.py"

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("pytest") is not None and importlib.util.find_spec("httpx") is not None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-design-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def target(self, name: str = "out") -> str:
        return str(self.tmp / name)


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #


class SpecValidationTests(unittest.TestCase):
    def test_every_valid_fixture_validates_without_errors(self) -> None:
        for path in sorted((SPECS / "valid").glob("*.json")) + [INVENTORY, ORDERS]:
            with self.subTest(spec=path.name):
                spec, errors = design.validate_spec(_load(path), str(path))
                self.assertEqual(errors, [])
                self.assertIsNotNone(spec)
                self.assertEqual(spec.schema, design.SPEC_SCHEMA)
                self.assertEqual(len(spec.digest), 64)

    def test_every_invalid_fixture_is_rejected_with_json_paths(self) -> None:
        for path in sorted((SPECS / "invalid").glob("*.json")):
            with self.subTest(spec=path.name):
                with self.assertRaises(design.SpecError) as ctx:
                    design.load_spec(str(path))
                err = ctx.exception
                self.assertEqual(err.source, str(path))
                self.assertTrue(err.errors)
                if path.name == "not_an_object.json":
                    self.assertEqual(err.errors, ["top level: expected an object"])
                    continue
                if path.name == "not_json.json":
                    self.assertIn("not valid JSON", err.errors[0])
                    continue
                expected = _load(path)["x-expect"]
                joined = "\n".join(err.errors)
                for fragment in expected:
                    self.assertIn(fragment, joined, msg=f"{path.name}: {fragment!r} not reported; got:\n{joined}")
                self.assertTrue(all(e.startswith("spec") for e in err.errors), err.errors)

    def test_errors_are_collected_not_short_circuited(self) -> None:
        _, errors = design.validate_spec(_load(SPECS / "invalid" / "inconsistent_choices.json"))
        self.assertGreaterEqual(len(errors), 7)

    def test_provenance_separates_explicit_values_from_assumptions(self) -> None:
        minimal = design.load_spec(str(SPECS / "valid" / "minimal.json"))
        self.assertEqual(minimal.provenance["spec.persistence.kind"], "assumed")
        self.assertEqual(minimal.provenance["spec.security.authentication"], "assumed")
        self.assertIn("spec.persistence.kind", minimal.assumed_paths())
        self.assertEqual(minimal.package, "notes")
        inventory = design.load_spec(str(INVENTORY))
        self.assertEqual(inventory.provenance["spec.persistence.kind"], "explicit")
        self.assertEqual(inventory.provenance["spec.security.authentication"], "explicit")
        self.assertNotIn("spec.persistence.kind", inventory.assumed_paths())

    def test_pending_decisions_and_open_questions_are_surfaced(self) -> None:
        spec = design.load_spec(str(SPECS / "valid" / "tenant_modular.json"))
        self.assertEqual(spec.pending_decisions(), ["decision D1: Reminder channel", "open question: Can a practitioner work in several clinics?"])

    def test_x_prefixed_annotations_are_accepted(self) -> None:
        spec, errors = design.validate_spec(_load(SPECS / "valid" / "tenant_modular.json"))
        self.assertEqual(errors, [])
        self.assertTrue(spec.multi_tenant)
        self.assertEqual(spec.security["multi_tenant"]["tenant_param"], "clinic_id")

    def test_digest_is_canonical_and_insensitive_to_key_order(self) -> None:
        doc = _load(INVENTORY)
        reordered = json.loads(json.dumps(doc, sort_keys=True))
        a, _ = design.validate_spec(doc)
        b, _ = design.validate_spec(reordered)
        self.assertEqual(a.digest, b.digest)
        doc["description"] = "changed"
        c, _ = design.validate_spec(doc)
        self.assertNotEqual(a.digest, c.digest)

    def test_operations_default_to_all_and_are_deduplicated(self) -> None:
        spec = design.load_spec(str(SPECS / "valid" / "minimal.json"))
        self.assertEqual(spec.modules[0].entities[0].operations, ["create", "get", "list", "update", "delete"])
        self.assertEqual(spec.provenance["spec.functional.modules[0].entities[0].operations"], "assumed")

    def test_to_dict_round_trips_through_json(self) -> None:
        spec = design.load_spec(str(ORDERS))
        text = json.dumps(spec.to_dict(), sort_keys=True)
        self.assertIn('"digest"', text)
        self.assertIn('"provenance"', text)


# --------------------------------------------------------------------------- #
# advisor
# --------------------------------------------------------------------------- #


class AdvisorTests(unittest.TestCase):
    def rec(self, path: Path):
        return design.recommend(design.load_spec(str(path)))

    def test_small_single_module_service_is_layered_with_reasons(self) -> None:
        rec = self.rec(INVENTORY)
        self.assertEqual(rec.style, "layered")
        self.assertEqual([r["style"] for r in rec.rejected], ["microservices", "hexagonal", "modular_monolith"])
        self.assertTrue(all(r["reason"] for r in rec.rejected))
        self.assertEqual(rec.warnings, [])

    def test_integrations_and_contract_tests_lead_to_hexagonal_only_for_integrating_modules(self) -> None:
        rec = self.rec(ORDERS)
        self.assertEqual(rec.style, "hexagonal")
        self.assertEqual(rec.ports_modules, ["billing", "orders"])
        self.assertIn("layered", [r["style"] for r in rec.rejected])

    def test_three_modules_or_medium_scope_lead_to_a_modular_monolith(self) -> None:
        rec = self.rec(SPECS / "valid" / "tenant_modular.json")
        self.assertEqual(rec.style, "modular_monolith")
        self.assertTrue(any("membership" in p for p in rec.pending))

    def test_microservices_only_when_independent_deployment_is_stated_and_justified(self) -> None:
        spec = design.load_spec(str(SPECS / "valid" / "microservices.json"))
        self.assertEqual(design.recommend(spec).style, "microservices")
        doc = _load(SPECS / "valid" / "microservices.json")
        doc["architecture"]["independent_deployment"] = False
        quieter, _ = design.validate_spec(doc)
        rec = design.recommend(quieter)
        self.assertNotEqual(rec.style, "microservices")
        self.assertIn("independent deployment is not stated", next(r["reason"] for r in rec.rejected if r["style"] == "microservices"))
        doc["architecture"]["independent_deployment"] = True
        doc["scope"]["team_size"] = 3
        doc["decisions"] = []
        small_team, _ = design.validate_spec(doc)
        self.assertNotEqual(design.recommend(small_team).style, "microservices")

    def test_explicit_preference_is_honoured_with_a_warning(self) -> None:
        rec = self.rec(SPECS / "valid" / "preference_override.json")
        self.assertEqual(rec.style, "hexagonal")
        self.assertTrue(rec.warnings and "prefers 'hexagonal'" in rec.warnings[0])
        self.assertIn("layered", rec.warnings[0])

    def test_unsupported_authentication_and_authorization_become_pending_decisions(self) -> None:
        rec = self.rec(SPECS / "valid" / "oauth_pending.json")
        self.assertTrue(any("oauth2_jwt" in p and "not generated" in p for p in rec.pending))
        self.assertTrue(any("authorization 'role'" in p for p in rec.pending))

    def test_postgres_without_alembic_eventual_consistency_and_broker_are_pending(self) -> None:
        rec = self.rec(SPECS / "valid" / "postgres_layered.json")
        self.assertTrue(any("outbox" in p for p in rec.pending))
        self.assertFalse(any("migrations are not enabled" in p for p in rec.pending))  # migrations: true in the fixture
        self.assertTrue(any("public" in n for n in rec.security_notes))

    def test_recommendation_is_deterministic(self) -> None:
        a = self.rec(ORDERS).to_dict()
        b = self.rec(ORDERS).to_dict()
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# --------------------------------------------------------------------------- #
# blueprint
# --------------------------------------------------------------------------- #


class BlueprintTests(unittest.TestCase):
    def blueprint(self, path: Path):
        spec = design.load_spec(str(path))
        return spec, design.build(spec, design.recommend(spec))

    def test_generated_python_parses_for_every_generatable_fixture(self) -> None:
        for path in sorted((SPECS / "valid").glob("*.json")) + [INVENTORY, ORDERS]:
            spec = design.load_spec(str(path))
            if design.generation_refusal(spec):
                continue
            with self.subTest(spec=path.name):
                bp = design.build(spec, design.recommend(spec))
                for rel, content in bp.files.items():
                    if rel.endswith(".py"):
                        ast.parse(content, filename=rel)
                    self.assertFalse(rel.startswith("/") or ".." in rel.split("/"), rel)
                json.loads(bp.files[".audit-fastapi.json"])

    def test_blueprint_is_deterministic(self) -> None:
        _, a = self.blueprint(ORDERS)
        _, b = self.blueprint(ORDERS)
        self.assertEqual(a.files, b.files)
        self.assertEqual(a.layout, b.layout)

    def test_layered_and_modular_layouts(self) -> None:
        _, layered = self.blueprint(INVENTORY)
        self.assertIn("src/inventory_service/api/catalog.py", layered.files)
        self.assertIn("src/inventory_service/services/catalog.py", layered.files)
        self.assertNotIn("src/inventory_service/modules/__init__.py", layered.files)
        _, modular = self.blueprint(SPECS / "valid" / "tenant_modular.json")
        self.assertIn("src/clinic/modules/appointments/router.py", modular.files)
        self.assertIn("src/clinic/modules/appointments/service.py", modular.files)
        self.assertNotIn("src/clinic/modules/appointments/ports.py", modular.files)
        self.assertIn("[tool.importlinter]", modular.files["pyproject.toml"])
        self.assertIn('"clinic.modules.practitioners"', modular.files["pyproject.toml"])

    def test_hexagonal_ports_and_adapters_only_where_an_integration_exists(self) -> None:
        _, bp = self.blueprint(ORDERS)
        self.assertIn("src/orders_platform/modules/orders/ports.py", bp.files)
        self.assertIn("src/orders_platform/modules/orders/adapters/payments.py", bp.files)
        self.assertIn("src/orders_platform/modules/billing/adapters/notifications.py", bp.files)
        self.assertNotIn("src/orders_platform/modules/customers/ports.py", bp.files)
        self.assertIn("class PaymentsPort(Protocol)", bp.files["src/orders_platform/modules/orders/ports.py"])
        self.assertIn("httpx.AsyncClient", bp.files["src/orders_platform/modules/orders/adapters/payments.py"])
        self.assertNotIn("httpx", bp.files["src/orders_platform/modules/orders/service.py"])

    def test_only_declared_operations_are_generated(self) -> None:
        _, bp = self.blueprint(ORDERS)
        router = bp.files["src/orders_platform/modules/orders/router.py"]
        self.assertIn("@router.post", router)
        self.assertIn("@router.get", router)
        self.assertNotIn("@router.patch", router)
        self.assertNotIn("@router.delete", router)
        self.assertNotIn("OrderUpdate", bp.files["src/orders_platform/modules/orders/schemas.py"])
        self.assertNotIn("async def delete", bp.files["src/orders_platform/modules/orders/repository.py"])
        tests = bp.files["tests/test_orders.py"]
        self.assertNotIn("test_update_order", tests)
        self.assertNotIn("test_delete_order", tests)
        self.assertIn("test_create_order_notifies_the_payments_port", tests)

    def test_audit_configuration_declares_the_design(self) -> None:
        _, public = self.blueprint(SPECS / "valid" / "postgres_layered.json")
        cfg = json.loads(public.files[".audit-fastapi.json"])
        self.assertEqual(cfg["public_routes"], ["/*"])
        self.assertEqual(cfg["contracts"]["commit_layers"], ["service"])
        self.assertNotIn("route_auth_required", cfg["contracts"])
        _, tenant = self.blueprint(SPECS / "valid" / "tenant_modular.json")
        cfg = json.loads(tenant.files[".audit-fastapi.json"])
        self.assertEqual(cfg["tenant_params"], ["clinic_id"])
        self.assertEqual(cfg["contracts"]["route_auth_required"], ["/api/*"])
        pairs = {(p["from"], p["to"]) for p in cfg["contracts"]["forbidden_imports"]}
        self.assertIn(("clinic.modules.practitioners", "clinic.modules.appointments"), pairs)
        self.assertNotIn(("clinic.modules.appointments", "clinic.modules.rooms"), pairs)  # declared dependency

    def test_tenant_scoping_reaches_routes_repositories_and_tests(self) -> None:
        _, bp = self.blueprint(SPECS / "valid" / "tenant_modular.json")
        self.assertIn('@router.post("/tenants/{clinic_id}/appointments"', bp.files["src/clinic/modules/appointments/router.py"])
        self.assertIn("item.clinic_id == clinic_id", bp.files["src/clinic/modules/appointments/repository.py"])
        self.assertIn("def appointment_url(tenant: uuid.UUID)", bp.files["tests/test_appointments.py"])

    def test_security_module_only_with_api_key_and_db_module_only_with_postgresql(self) -> None:
        _, memory_public = self.blueprint(SPECS / "valid" / "minimal.json")
        self.assertNotIn("src/notes/core/security.py", memory_public.files)
        self.assertNotIn("src/notes/core/db.py", memory_public.files)
        self.assertNotIn("Depends(require_api_key)", memory_public.files["src/notes/api/router.py"])
        _, pg = self.blueprint(SPECS / "valid" / "postgres_layered.json")
        self.assertIn("src/readings/core/db.py", pg.files)
        self.assertIn("asyncpg", pg.files["pyproject.toml"])
        self.assertIn('pytest.skip("set DATABASE_URL', pg.files["tests/conftest.py"])
        self.assertIn("await self._session.commit()", pg.files["src/readings/services/sensors.py"])


# --------------------------------------------------------------------------- #
# generator
# --------------------------------------------------------------------------- #


class GeneratorTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.spec = design.load_spec(str(INVENTORY))
        self.bp = design.build(self.spec, design.recommend(self.spec))

    def gen(self, target: str, **kwargs):
        return design.generate(self.spec, self.bp, target, tool_version=TOOL_VERSION, **kwargs)

    def test_generation_into_a_new_directory_writes_everything_and_a_manifest(self) -> None:
        result = self.gen(self.target())
        self.assertEqual(sorted(result.written), sorted(list(self.bp.files) + [design.MANIFEST]))
        self.assertEqual(result.unchanged, [])
        manifest = json.loads((Path(result.target) / design.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], design.MANIFEST_SCHEMA)
        self.assertEqual(manifest["spec"]["digest"], self.spec.digest)
        self.assertEqual(manifest["tool"]["version"], TOOL_VERSION)
        for rel, digest in manifest["files"].items():
            self.assertEqual(_sha((Path(result.target) / rel).read_text(encoding="utf-8")), digest)
        self.assertNotIn("time", json.dumps(manifest).lower())
        status = design.verify_manifest(result.target)["status"]
        self.assertEqual(set(status.values()), {"identical"})

    def test_second_run_is_idempotent(self) -> None:
        self.gen(self.target())
        before = {p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        again = self.gen(self.target())
        self.assertEqual(again.written, [])
        self.assertEqual(sorted(again.unchanged), sorted(list(self.bp.files) + [design.MANIFEST]))
        after = {p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_manifest_is_reproducible_across_directories(self) -> None:
        a = self.gen(self.target("a"))
        b = self.gen(self.target("b"))
        self.assertEqual((Path(a.target) / design.MANIFEST).read_text(), (Path(b.target) / design.MANIFEST).read_text())

    def test_non_empty_target_is_refused_without_allow_existing(self) -> None:
        target = Path(self.target())
        target.mkdir()
        (target / "notes.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(design.GenerationError) as ctx:
            self.gen(str(target))
        self.assertIn("not empty", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["notes.txt"])  # nothing written

    def test_allow_existing_adds_only_missing_files(self) -> None:
        result = self.gen(self.target())
        removed = Path(result.target) / "tests" / "test_catalog.py"
        removed.unlink()
        (Path(result.target) / "notes.txt").write_text("mine", encoding="utf-8")
        again = self.gen(self.target(), allow_existing=True)
        self.assertEqual(again.written, ["tests/test_catalog.py"])
        self.assertTrue(removed.is_file())
        self.assertEqual((Path(result.target) / "notes.txt").read_text(encoding="utf-8"), "mine")

    def test_differing_file_is_a_conflict_and_is_never_replaced_without_overwrite(self) -> None:
        result = self.gen(self.target())
        edited = Path(result.target) / "src" / "inventory_service" / "services" / "catalog.py"
        edited.write_text("# hand-edited\n", encoding="utf-8")
        with self.assertRaises(design.GenerationError) as ctx:
            self.gen(self.target(), allow_existing=True)
        self.assertIn("differ", str(ctx.exception))
        self.assertIn("services/catalog.py", str(ctx.exception))
        self.assertEqual(edited.read_text(encoding="utf-8"), "# hand-edited\n")
        self.assertEqual(design.verify_manifest(result.target)["status"]["src/inventory_service/services/catalog.py"], "modified")

    def test_overwrite_replaces_only_differing_files_and_deletes_nothing(self) -> None:
        result = self.gen(self.target())
        edited = Path(result.target) / "src" / "inventory_service" / "services" / "catalog.py"
        edited.write_text("# hand-edited\n", encoding="utf-8")
        extra = Path(result.target) / "docs.md"
        extra.write_text("keep me", encoding="utf-8")
        again = self.gen(self.target(), overwrite=True)
        self.assertEqual(again.written, ["src/inventory_service/services/catalog.py"])
        self.assertEqual(edited.read_text(encoding="utf-8"), self.bp.files["src/inventory_service/services/catalog.py"])
        self.assertTrue(extra.is_file())

    def test_dry_run_touches_nothing(self) -> None:
        result = self.gen(self.target(), dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertTrue(result.written)
        self.assertFalse(Path(self.target()).exists())

    def test_symlinked_or_file_targets_are_refused(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        os.symlink(real, link)
        with self.assertRaises(design.GenerationError):
            self.gen(str(link))
        plain = self.tmp / "file.txt"
        plain.write_text("x", encoding="utf-8")
        with self.assertRaises(design.GenerationError):
            self.gen(str(plain))

    def test_paths_that_escape_the_target_are_refused(self) -> None:
        from fastapi_design.generator import _safe_join

        for bad in ("../outside.py", "/etc/passwd", "a/../../b", " spaced", "win\\path"):
            with self.subTest(path=bad):
                with self.assertRaises(design.GenerationError):
                    _safe_join(self.tmp, bad)
        self.assertEqual(_safe_join(self.tmp, "src/pkg/x.py"), self.tmp / "src" / "pkg" / "x.py")

    def test_a_directory_in_the_way_of_a_file_is_an_error(self) -> None:
        target = Path(self.target())
        (target / "README.md").mkdir(parents=True)
        with self.assertRaises(design.GenerationError):
            self.gen(str(target), allow_existing=True)


# --------------------------------------------------------------------------- #
# pipeline, verification and report
# --------------------------------------------------------------------------- #


class PipelineTests(TempDirCase):
    def test_validate_only_reports_without_generating(self) -> None:
        report = design.run_design(str(INVENTORY), design.DesignOptions(validate_only=True))
        self.assertEqual(report["schema"], design.REPORT_SCHEMA)
        self.assertEqual(report["tool"]["version"], TOOL_VERSION)
        self.assertIsNone(report["generation"])
        self.assertIsNone(report["verification"])
        self.assertEqual(report["recommendation"]["style"], "layered")
        self.assertEqual(report["spec"]["digest"], design.load_spec(str(INVENTORY)).digest)
        self.assertTrue(report["next_steps"])

    def test_unsupported_authentication_validates_but_generation_is_refused(self) -> None:
        path = SPECS / "valid" / "oauth_pending.json"
        report = design.run_design(str(path), design.DesignOptions(validate_only=True))
        self.assertIn("oauth2_jwt", report["generation_refused"])
        with self.assertRaises(design.DesignError):
            design.run_design(str(path), design.DesignOptions(target=self.target()))
        self.assertFalse(Path(self.target()).exists())

    def test_generated_inventory_service_audits_clean_and_runs_nothing_by_default(self) -> None:
        report = design.run_design(str(INVENTORY), design.DesignOptions(target=self.target()))
        audit = report["verification"]["audit"]
        self.assertEqual(audit["findings_total"], 0, audit["findings"])
        self.assertGreater(audit["files_scanned"], 10)
        self.assertEqual(audit["contracts"]["unauthenticated_not_public"], 0)
        self.assertEqual(audit["contracts"]["authenticated"], 5)
        self.assertIsNone(report["verification"]["tests"])
        self.assertFalse(report["authorization"]["execution"])
        self.assertIn("src/inventory_service/api/catalog.py", report["files"])

    def test_generated_orders_platform_audits_with_only_positive_signals(self) -> None:
        report = design.run_design(str(ORDERS), design.DesignOptions(target=self.target()))
        audit = report["verification"]["audit"]
        self.assertEqual([f["rule"] for f in audit["findings"]], ["ARCH_IMPORT_LINTER_PRESENT"])
        self.assertEqual(audit["by_severity"], {"info": 1})
        self.assertFalse(audit["partial_analysis"])
        self.assertEqual(audit["contracts"]["unauthenticated_not_public"], 0)
        self.assertEqual(report["recommendation"]["style"], "hexagonal")

    def test_every_generatable_fixture_generates_and_audits_without_high_findings(self) -> None:
        for path in sorted((SPECS / "valid").glob("*.json")):
            spec = design.load_spec(str(path))
            if design.generation_refusal(spec):
                continue
            with self.subTest(spec=path.name):
                report = design.run_design(spec, design.DesignOptions(target=self.target(path.stem)))
                audit = report["verification"]["audit"]
                self.assertNotIn("high", audit["by_severity"], audit["findings"])
                self.assertNotIn("critical", audit["by_severity"], audit["findings"])
                self.assertEqual(audit["contracts"]["unauthenticated_not_public"], 0, audit["findings"])

    def test_untrusted_authorization_fails_closed(self) -> None:
        options = design.DesignOptions(target=self.target(), authorization=Authorization(execution=True, trust="untrusted", scope=self.target()), policy=ExecutionPolicy(authorized=True), test_command="python3 -m pytest -q")
        report = design.run_design(str(INVENTORY), options)
        tests = report["verification"]["tests"]
        self.assertFalse(tests["ran"])
        self.assertEqual(tests["denied_code"], "executor_unavailable")
        self.assertIn("not isolation", tests["denied_reason"])

    def test_scope_outside_the_target_fails_closed(self) -> None:
        elsewhere = str(self.tmp / "elsewhere")
        options = design.DesignOptions(target=self.target(), authorization=Authorization(execution=True, trust="fixture", scope=elsewhere), policy=ExecutionPolicy(authorized=True), test_command="python3 -m pytest -q")
        report = design.run_design(str(INVENTORY), options)
        self.assertEqual(report["verification"]["tests"]["denied_code"], "scope_mismatch")

    def test_disallowed_program_is_refused_by_the_executor(self) -> None:
        options = design.DesignOptions(target=self.target(), authorization=Authorization(execution=True, trust="fixture", scope=self.target()), policy=ExecutionPolicy(authorized=True), test_command="bash -c 'echo hi'")
        report = design.run_design(str(INVENTORY), options)
        tests = report["verification"]["tests"]
        self.assertFalse(tests["ran"])
        self.assertIn("not in the allowed list", tests["run"]["refused"])
        self.assertEqual(tests["run"]["outcome"], "refused")

    @unittest.skipUnless(HAS_FASTAPI, "fastapi, httpx and pytest are not importable by this interpreter")
    def test_generated_tests_pass_through_the_trust_model(self) -> None:
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        options = design.DesignOptions(target=self.target(), authorization=Authorization(execution=True, trust="fixture", scope=self.target(), declared_by="tests: fixture generated by this suite"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=300), test_command=command)
        report = design.run_design(str(INVENTORY), options)
        tests = report["verification"]["tests"]
        self.assertTrue(tests["ran"], tests)
        run = tests["run"]
        self.assertEqual(run["outcome"], "passed", run.get("stdout_tail"))
        self.assertEqual(run["runner"], "pytest-junit")
        self.assertEqual(run["counts"]["failed"], 0)
        self.assertEqual(run["counts"]["errors"], 0)
        self.assertGreaterEqual(run["counts"]["passed"], 9)
        self.assertEqual(tests["executor"]["isolation_level"], "process_hygiene")
        self.assertTrue(run["evidence"]["matched"], run["evidence"])  # the junit report carries the nonce of this run
        self.assertTrue(run["evidence"]["verifiable"])
        self.assertEqual(design.verify_manifest(self.target())["status"]["src/inventory_service/main.py"], "identical")  # the run did not modify the project

    def test_report_renderings_cover_recommendation_generation_and_verification(self) -> None:
        report = design.run_design(str(ORDERS), design.DesignOptions(target=self.target()))
        text = design.render_text(report)
        md = design.render_markdown(report)
        for rendering in (text, md):
            self.assertIn("hexagonal", rendering)
            self.assertIn("ARCH_IMPORT_LINTER_PRESENT", rendering)
            self.assertIn("Payment provider selection", rendering)
        self.assertIn("## Layout", md)
        self.assertIn("not executed" , text.lower()) if report["verification"]["tests"] else None
        payload = design.to_json(report)
        self.assertEqual(json.loads(payload)["schema"], design.REPORT_SCHEMA)
        self.assertEqual(payload, design.to_json(json.loads(payload)))


class SnapshotTests(TempDirCase):
    def test_committed_example_equals_a_fresh_generation(self) -> None:
        self.assertTrue(SNAPSHOT.is_dir(), f"missing committed example at {SNAPSHOT}")
        spec = design.load_spec(str(INVENTORY))
        bp = design.build(spec, design.recommend(spec))
        result = design.generate(spec, bp, self.target(), tool_version=TOOL_VERSION)
        fresh = {str(p.relative_to(result.target)): p.read_text(encoding="utf-8") for p in Path(result.target).rglob("*") if p.is_file()}
        committed = {str(p.relative_to(SNAPSHOT)): p.read_text(encoding="utf-8") for p in SNAPSHOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(sorted(committed), sorted(fresh))
        for rel in fresh:
            self.assertEqual(committed[rel], fresh[rel], f"{rel} differs from a fresh generation")
        self.assertEqual(set(design.verify_manifest(str(SNAPSHOT))["status"].values()), {"identical"})

    def test_committed_example_audits_clean(self) -> None:
        audit = design.audit_generated(str(SNAPSHOT))
        self.assertEqual(audit["findings_total"], 0, audit["findings"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class CliTests(TempDirCase):
    def run_cli(self, *args: str, entry: Path = ROOT_CLI):
        proc = subprocess.run([sys.executable, str(entry), *args], capture_output=True, text=True, cwd=str(self.tmp), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return proc.returncode, proc.stdout, proc.stderr

    def test_validate_only_through_both_entry_points(self) -> None:
        for entry in (ROOT_CLI, SKILL_CLI):
            code, out, err = self.run_cli(str(INVENTORY), "--validate-only", entry=entry)
            self.assertEqual(code, 0, err)
            self.assertIn("style: layered", out)
            self.assertIn("pending decisions: 1", out)

    def test_invalid_specification_exits_2_with_every_error(self) -> None:
        code, out, err = self.run_cli(str(SPECS / "invalid" / "inconsistent_choices.json"), "--validate-only")
        self.assertEqual(code, 2)
        self.assertIn("invalid specification", err)
        self.assertIn("spec.security.authorization", err)
        self.assertIn("spec.persistence.migrations", err)

    def test_argument_guards(self) -> None:
        self.assertEqual(self.run_cli(str(INVENTORY))[0], 2)  # no --target
        self.assertEqual(self.run_cli(str(INVENTORY), "--target", self.target(), "--test-command", "pytest")[0], 2)  # without --authorize-execution
        self.assertEqual(self.run_cli(str(INVENTORY), "--target", self.target(), "--authorize-execution", "--trust", "fixture")[0], 2)  # without --test-command
        self.assertFalse(Path(self.target()).exists())

    def test_generation_json_report_strict_and_conflict_exit_codes(self) -> None:
        code, out, err = self.run_cli(str(INVENTORY), "--target", self.target(), "--format", "json", "--strict")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], design.REPORT_SCHEMA)
        self.assertEqual(report["verification"]["audit"]["findings_total"], 0)
        self.assertTrue((Path(self.target()) / design.MANIFEST).is_file())
        code, _, err = self.run_cli(str(INVENTORY), "--target", self.target(), "--allow-existing")
        self.assertEqual(code, 0, err)
        (Path(self.target()) / "README.md").write_text("edited\n", encoding="utf-8")
        code, _, err = self.run_cli(str(INVENTORY), "--target", self.target(), "--allow-existing")
        self.assertEqual(code, 2)
        self.assertIn("README.md", err)
        self.assertEqual((Path(self.target()) / "README.md").read_text(encoding="utf-8"), "edited\n")
        code, _, err = self.run_cli(str(INVENTORY), "--target", self.target(), "--overwrite", "--no-audit", "--output", str(self.tmp / "r.json"))
        self.assertEqual(code, 0, err)
        self.assertNotEqual((Path(self.target()) / "README.md").read_text(encoding="utf-8"), "edited\n")
        self.assertIsNone(json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))["verification"]["audit"])

    def test_dry_run_and_markdown(self) -> None:
        code, out, err = self.run_cli(str(ORDERS), "--target", self.target(), "--dry-run", "--format", "markdown")
        self.assertEqual(code, 0, err)
        self.assertIn("(dry run)", out)
        self.assertIn("## Recommended style: `hexagonal`", out)
        self.assertFalse(Path(self.target()).exists())

    def test_untrusted_execution_is_denied_and_strict_fails(self) -> None:
        code, out, err = self.run_cli(str(INVENTORY), "--target", self.target(), "--authorize-execution", "--trust", "untrusted", "--trust-scope", self.target(), "--test-command", "python3 -m pytest -q", "--strict")
        self.assertEqual(code, 1, err)
        self.assertIn("tests: not executed", out)


if __name__ == "__main__":
    unittest.main()
