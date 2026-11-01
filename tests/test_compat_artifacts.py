"""v2.1, increment 0: artefacts written by earlier versions are still read and still mean what they meant.

The fixtures under ``tests/fixtures/compat`` are frozen outputs of v2.0.0 (see its README for the provenance): a
project with a manifest 1.0 and the contract it was generated from, a project with a manifest 1.1, evolution
contracts 1.0 and 1.1, an operation journal 1.0 interrupted during publication, an adoption baseline 1.0 and an audit
report 1.7 with its fingerprints; and, since v2.2 increment 0, the outputs of v2.1.0: a project with a manifest 1.3,
the two phased evolution contracts 1.2, a migration evidence 1.0 and an evolution report 1.3. Nothing here
regenerates them.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.engine import AuditOptions, audit_project  # noqa: E402
from fastapi_audit.report import compare_reports, fingerprint_of  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design import journal  # noqa: E402
from fastapi_design.adoption import read_adoption  # noqa: E402
from fastapi_design.evolution import EvolutionContract, EvolveOptions, load_evolution, plan_evolution, run_evolution, validate_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402
from fastapi_design.migrations import contract_preconditions, load_evidence  # noqa: E402

COMPAT = FIXTURES / "compat"
PROJECT_10 = COMPAT / "generation-1.0" / "inventory-service"
SPEC_10 = COMPAT / "generation-1.0" / "inventory-service.spec.json"
PROJECT_11 = COMPAT / "generation-1.1" / "inventory-service"
PROJECT_13 = COMPAT / "generation-1.3" / "parcels"
HANDMADE = FIXTURES / "adoption20" / "handmade_shop"

SUPPLIERS = {"name": "suppliers", "description": "Suppliers of the products", "entities": [{"name": "Supplier", "fields": [{"name": "name", "type": "str", "max_length": 120}], "operations": ["create", "get", "list"]}]}


def _contract(project: Path, **overrides) -> EvolutionContract:
    base = dict(schema="fastapi-architect/evolution/1.1", project_path=str(project), package=None, expected_spec_digest=None, baseline_contract=None, add_modules=[SUPPLIERS], modify_modules=[], invariants=["existing_routes_unchanged"], allow_modify=None, preserve=[], source=None, document={})
    base.update(overrides)
    return EvolutionContract(**base)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-compat-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target


class ManifestCompatibilityTests(TempCase):
    def test_a_manifest_10_project_is_discovered_and_its_files_are_managed(self) -> None:
        manifest = read_manifest(str(PROJECT_10))
        self.assertEqual(manifest["schema"], "fastapi-architect/generation/1.0")
        self.assertNotIn("spec_document", manifest)
        model = design.discover(str(PROJECT_10), audit=False)
        self.assertEqual((model.package, model.layout), ("inventory_service", "layered"))
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"})
        self.assertEqual(set(design.verify_manifest(str(PROJECT_10))["status"].values()), {"identical"})

    def test_a_manifest_10_project_blocks_without_its_contract_and_plans_with_it(self) -> None:
        blocked = design.plan_evolution(_contract(PROJECT_10))
        self.assertEqual(blocked.state, "blocked")
        self.assertTrue(any(b.startswith("baseline_contract_available") for b in blocked.blockers), blocked.blockers)
        planned = design.plan_evolution(_contract(PROJECT_10), baseline_contract=str(SPEC_10))
        self.assertEqual(planned.state, "planned", planned.blockers)
        self.assertEqual(planned.conflicts, [])
        self.assertIn("suppliers", planned.added_modules)
        stale = self.tmp / "stale.json"
        doc = json.loads(SPEC_10.read_text(encoding="utf-8"))
        doc["description"] = "edited after generation"
        stale.write_text(json.dumps(doc), encoding="utf-8")
        wrong = design.plan_evolution(_contract(PROJECT_10), baseline_contract=str(stale))
        self.assertEqual(wrong.state, "blocked")
        self.assertTrue(any(b.startswith("baseline_contract_matches_manifest") for b in wrong.blockers), wrong.blockers)

    def test_a_manifest_11_project_plans_and_verifies_in_a_copy(self) -> None:
        manifest = read_manifest(str(PROJECT_11))
        self.assertEqual(manifest["schema"], "fastapi-architect/generation/1.1")
        model = design.discover(str(PROJECT_11), audit=False)
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"})
        plan = design.plan_evolution(_contract(PROJECT_11))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.conflicts, [])
        self.assertFalse(plan.migration["required"])
        report = design.run_evolution(_contract(PROJECT_11), design.EvolveOptions(verify=True))
        self.assertEqual(report["verification"]["state"], "audit_clean", report["verification"]["reasons"])
        self.assertEqual(report["verification"]["routes"]["missing"], [])
        self.assertEqual(report["verification"]["protected_files"]["unexpectedly_changed"], [])
        self.assertEqual(report["applied"]["state"], "not_applied")
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"}, "the fixture itself was never written")


class EvolutionContractCompatibilityTests(TempCase):
    def test_contracts_10_and_11_are_accepted_and_plan_against_the_frozen_project(self) -> None:
        for name, schema in (("evolution-1.0.json", "fastapi-architect/evolution/1.0"), ("evolution-1.1.json", "fastapi-architect/evolution/1.1")):
            with self.subTest(contract=name):
                contract = design.load_evolution(str(COMPAT / name))
                self.assertEqual(contract.schema, schema)
                plan = design.plan_evolution(contract)
                self.assertEqual(plan.state, "planned", plan.blockers)
                self.assertEqual(plan.conflicts, [])

    def test_a_10_contract_still_rejects_what_10_never_had(self) -> None:
        doc = json.loads((COMPAT / "evolution-1.0.json").read_text(encoding="utf-8"))
        doc["modify_modules"] = [{"name": "products", "entities": []}]
        contract, errors = validate_evolution(doc, str(COMPAT / "evolution-1.0.json"))
        self.assertIsNone(contract)
        self.assertIn("evolution.modify_modules: needs schema fastapi-architect/evolution/1.1", errors)


class JournalCompatibilityTests(TempCase):
    def _interrupted(self) -> Path:
        return self.copy(COMPAT / "operation-1.0" / "project", "project")

    def test_an_interrupted_journal_10_is_read_and_classified(self) -> None:
        project = self._interrupted()
        pending = journal.pending_operations(project)
        self.assertEqual([op.state for op in pending], ["publishing"])
        self.assertEqual(journal.unreadable_operations(project), [])
        op = pending[0]
        self.assertEqual(op.kind, "evolve")
        self.assertEqual([f.path for f in op.files], ["a.txt", "b.txt"])
        self.assertEqual(journal.classify(project, op), {"a.txt": "published", "b.txt": "pending"})
        plan = design.plan_evolution(_contract(project))
        self.assertEqual(plan.state, "blocked")
        self.assertTrue(any(b.startswith("no_pending_operation") for b in plan.blockers), plan.blockers)

    def test_forward_and_rollback_still_apply_to_a_journal_10(self) -> None:
        forward = self._interrupted()
        report = journal.recover(forward, journal.pending_operations(forward)[0], strategy="forward")
        self.assertEqual(report.state, "completed")
        self.assertEqual(((forward / "a.txt").read_text(encoding="utf-8"), (forward / "b.txt").read_text(encoding="utf-8")), ("a1\n", "b1\n"))
        self.assertEqual(journal.pending_operations(forward), [])
        shutil.rmtree(forward)
        rollback = self._interrupted()
        report = journal.recover(rollback, journal.pending_operations(rollback)[0], strategy="rollback")
        self.assertEqual(report.state, "rolled_back")
        self.assertEqual(((rollback / "a.txt").read_text(encoding="utf-8"), (rollback / "b.txt").read_text(encoding="utf-8")), ("a0\n", "b0\n"))
        self.assertEqual(journal.pending_operations(rollback), [])


class AdoptionCompatibilityTests(TempCase):
    def test_an_adoption_baseline_10_is_read_and_its_files_are_adopted_unchanged(self) -> None:
        project = self.copy(HANDMADE, "handmade_shop")
        (project / ".fastapi-architect").mkdir()
        shutil.copyfile(COMPAT / "adoption-1.0.json", project / ".fastapi-architect" / "adoption.json")
        baseline = read_adoption(project)
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["schema"], "fastapi-architect/adoption/1.0")
        self.assertIn("items", baseline["modules"])
        model = design.discover(str(project), audit=False)
        adopted = {rel: state for rel, state in model.ownership.items() if state.startswith("adopted")}
        self.assertTrue(adopted, model.ownership)
        self.assertEqual(set(adopted.values()), {"adopted_unchanged"}, adopted)


class AuditReportCompatibilityTests(unittest.TestCase):
    def test_fingerprints_of_a_17_report_are_reproduced_by_the_current_auditor(self) -> None:
        frozen = json.loads((COMPAT / "audit-report-1.7.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["schema_version"], "1.7")
        current = audit_project(str(FIXTURES / "sa_sessions"), options=AuditOptions())
        key = lambda f: (f["rule"], f["file"], f["line"], f["fingerprint"])  # noqa: E731
        self.assertEqual(sorted(key(f) for f in current["findings"]), sorted(key(f) for f in frozen["findings"]))
        for finding in frozen["findings"]:
            self.assertEqual(fingerprint_of({k: v for k, v in finding.items() if k != "fingerprint"}), finding["fingerprint"], finding["rule"])
        comparison = compare_reports(current, frozen, None)
        self.assertEqual(comparison["new"], [])
        self.assertEqual(comparison["no_longer_detected"], [])
        self.assertGreaterEqual(float(current["schema_version"]), 1.7)


if __name__ == "__main__":
    unittest.main()


class V21ArtefactCompatibilityTests(TempCase):
    """What v2.1.0 wrote is still read and still means the same: the manifest 1.3 project, the phased contracts 1.2,
    the migration evidence 1.0 and the evolution report 1.3."""

    def test_a_manifest_13_project_is_discovered_and_plans_its_phased_expand(self) -> None:
        project = self.copy(PROJECT_13, "parcels")
        manifest = read_manifest(str(project))
        self.assertEqual((manifest["schema"], manifest["tool"]["version"], manifest["phased_changes"], [m["phase"] for m in manifest["migrations"]]), ("fastapi-architect/generation/1.3", "2.1.0", [], ["initial"]))
        model = design.discover(str(project), audit=False)
        self.assertEqual(set(model.ownership.values()), {"managed_unchanged"})
        doc = json.loads((COMPAT / "evolution-1.2-expand.json").read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        contract_path = self.tmp / "expand.json"
        contract_path.write_text(json.dumps(doc), encoding="utf-8")
        plan = plan_evolution(load_evolution(str(contract_path)))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual([p["phase"] for p in plan.migration["phases"]], ["expand", "index"])
        self.assertEqual(sorted(b["column"] for b in plan.migration["backfills"]), ["carrier", "tracking_token"])
        frozen = json.loads((COMPAT / "evolution-report-1.3.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["schema"], "evolution_report/1.3")
        self.assertEqual([p["revision"] for p in frozen["plan"]["migration"]["phases"]], [p["revision"] for p in plan.migration["phases"]], "the same contract on the same project still yields the same revisions")
        self.assertEqual({b["operation"] for b in frozen["plan"]["migration"]["backfills"]}, {b["operation"] for b in plan.migration["backfills"]})
        self.assertIn("contract_authorization", frozen)

    def test_a_contract_12_contract_phase_is_still_refused_without_authorization_and_with_stale_evidence(self) -> None:
        project = self.copy(PROJECT_13, "parcels")
        doc = json.loads((COMPAT / "evolution-1.2-contract.json").read_text(encoding="utf-8"))
        doc["project"]["path"] = str(project)
        doc["authorization"]["contract"]["evidence"] = str(COMPAT / "migration-evidence-1.0.json")
        contract_path = self.tmp / "contract.json"
        contract_path.write_text(json.dumps(doc), encoding="utf-8")
        unknown = plan_evolution(load_evolution(str(contract_path)))
        self.assertEqual(unknown.state, "blocked")
        self.assertTrue(any(b.startswith("contract_operation_known") for b in unknown.blockers), unknown.blockers)  # the baseline has no phased change yet
        expand = json.loads((COMPAT / "evolution-1.2-expand.json").read_text(encoding="utf-8"))
        expand["project"]["path"] = str(project)
        expand_path = self.tmp / "expand.json"
        expand_path.write_text(json.dumps(expand), encoding="utf-8")
        applied = run_evolution(str(expand_path), EvolveOptions(apply=True, verify=False))
        self.assertEqual(applied["applied"]["state"], "applied", applied["plan"]["blockers"])
        self.assertEqual({c["op_id"] for c in read_manifest(str(project))["phased_changes"]}, {"66636e4277641692", "5821fb0e87d4b355"}, "the operation ids of v2.1.0 are reproduced")
        blocked = plan_evolution(load_evolution(str(contract_path)))
        self.assertEqual(blocked.state, "blocked")
        self.assertTrue(any(b.startswith("contract_authorized") for b in blocked.blockers), blocked.blockers)
        authorised = plan_evolution(load_evolution(str(contract_path)), authorize_contract=True)
        self.assertEqual(authorised.state, "blocked")
        self.assertTrue(any(b.startswith("contract_evidence_fresh") for b in authorised.blockers), authorised.blockers)

    def test_a_migration_evidence_10_is_loaded_and_evaluated_as_before(self) -> None:
        evidence, errors = load_evidence(str(COMPAT / "migration-evidence-1.0.json"))
        self.assertEqual((errors, evidence["schema"]), ([], "fastapi-architect/migration-evidence/1.0"))
        op_id, entry = next(iter(evidence["operations"].items()))
        change = {"op_id": op_id, "table": entry["table"], "column": entry["column"], "state_declared": "expand", "application_version": evidence["application_version_declared"]}
        checks = {name: ok for name, ok, _detail in contract_preconditions(change, evidence, head_before=evidence["expected_head"], authorized=True, declared_by="compat test", max_age_hours=24)}
        self.assertFalse(checks["contract_evidence_fresh"], "the frozen sample is stale by design")
        self.assertTrue(checks["contract_authorized"] and checks["contract_operation_pending"])

    def test_the_12_contract_keys_are_still_rejected_under_11(self) -> None:
        doc = json.loads((COMPAT / "evolution-1.2-expand.json").read_text(encoding="utf-8"))
        doc["schema"] = "fastapi-architect/evolution/1.1"
        _contract, errors = validate_evolution(doc, source=None)
        self.assertTrue(any("backfill" in e for e in errors), errors)
