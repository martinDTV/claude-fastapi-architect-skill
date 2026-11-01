"""Renames, second and third evolutions (v2.2, increment 2): retire (the truth moves to the new column, code state C)
and the destructive contract (the old column, its sync trigger and function are dropped), without a database.

The database-side behaviour (B and C as separate processes, the retire and drop revisions on a real PostgreSQL, the
downgrade that recreates the column) is in ``test_renames_v22_postgresql.py`` (opt-in). Here: the contract 1.4, the
plan-level preconditions one by one against synthetic evidence, the generated code of C, the revisions' text, the
manifest 1.5, the application tag, and the lint of what is generated.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design.blueprint.phased import DROP_PRECONDITIONS, application_tag, confirmation_phrase  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, load_evolution, plan_evolution, run_evolution, validate_evolution  # noqa: E402
from fastapi_design.generator import read_manifest  # noqa: E402
from fastapi_design.locking import acquire  # noqa: E402
from fastapi_design.migrations import load_evidence  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PARCELS = EXAMPLES / "generated" / "parcels"
RENAME_EXAMPLE = EXAMPLES / "evolutions" / "parcels-rename-label.json"
RETIRE_EXAMPLE = EXAMPLES / "evolutions" / "parcels-retire-label.json"
CONTRACT_EXAMPLE = EXAMPLES / "evolutions" / "parcels-contract-label.json"
INVARIANTS = ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"]
NON_DESTRUCTIVE = [i for i in INVARIANTS if i != "no_destructive_schema_change"]
RUFF = ["uvx", "ruff@0.17.3", "check", ".", "--output-format", "concise"]


def _evidence(ops: Dict[str, dict], *, state: str, truth_new: bool, head: str, tag: str, version: Optional[str], retired_at: Optional[str] = None, now: Optional[dt.datetime] = None) -> dict:
    """What ``verify`` would write for the parcels renames in a given state, with every condition satisfied."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ev = {
        "schema": "fastapi-architect/migration-evidence/1.2", "generated_at": now.isoformat(), "expected_head": head, "application_version_declared": version,
        "database": {"name": "x", "user": "fa_owner", "server_version": "16", "bypasses_rls": True}, "alembic": {"current": [head], "matches_expected_head": True},
        "operations": {}, "indexes": [], "problems": [], "ok": True, "tool_session": "parcels.ops.backfill",
        "sessions": [{"application_name": tag, "user": "app", "count": 2, "oldest_backend_start": now.isoformat(), "identified": True}],
    }
    for col, op in ops.items():
        old = op["from_column"]
        typ = "character varying" if col == "label" else "numeric"
        nullable_old = col == "weight"
        ev["operations"][op["op_id"]] = {
            "table": "shipments", "column": col, "kind": "rename", "source_column": old, "state_declared": state, "column_present": True, "source_present": True,
            "nullable": nullable_old or state == "expand", "pending_rows": 0, "mismatch_rows": 0, "columns_agree": True, "backfill_complete": True,
            "columns": {old: {"present": True, "nullable": nullable_old, "type": typ, "default": None}, col: {"present": True, "nullable": nullable_old or state == "expand", "type": typ, "default": None}},
            "progress": {"state": "complete", "rows_processed": 10, "batches": 1},
            "sync": {"mechanism": "trigger", "trigger": op["sync"]["trigger"], "function": op["sync"]["function"], "present": True, "enabled": True, "truth": col if truth_new else old, "retired_at": retired_at},
            "dependents": [], "unmanaged_dependents": [], "other_triggers": [], "functions_mentioning": [],
        }
    return ev


class Scenario:
    """A -> B (rename) -> C (retire) -> D (drop) on copies of the committed parcels snapshot, built once per class."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.now = dt.datetime.now(dt.timezone.utc)
        self.project = root / "parcels"
        shutil.copytree(PARCELS, self.project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        self.a_tag = read_manifest(str(self.project))["application_tag"]
        doc = json.loads(RENAME_EXAMPLE.read_text(encoding="utf-8"))
        doc["project"]["path"] = str(self.project)
        report = run_evolution(str(self.write("rename.json", doc)), EvolveOptions(apply=True, verify=False))
        assert report["applied"]["state"] == "applied", report["plan"]["blockers"]
        self.b_manifest = read_manifest(str(self.project))
        self.b_project = root / "parcels-b"
        shutil.copytree(self.project, self.b_project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        self.ops = {c["column"]: c for c in self.b_manifest["phased_changes"]}
        self.b_tag = self.b_manifest["application_tag"]
        self.b_head = self.b_manifest["migrations"][-1]["revision"]
        self.b_evidence = self.write("ev-b.json", _evidence(self.ops, state="expand", truth_new=False, head=self.b_head, tag=self.b_tag, version=self.ops["label"]["application_version"], now=self.now))
        self.retire_doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(self.project)}, "authorization": {"contract": {"declared_by": "tests", "evidence": str(self.b_evidence)}}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "retire_fields": [{"name": "label", "operation": self.ops["label"]["op_id"]}, {"name": "weight", "operation": self.ops["weight"]["op_id"]}]}]}], "invariants": INVARIANTS}
        self.retire_report = run_evolution(str(self.write("retire.json", self.retire_doc)), EvolveOptions(apply=True, verify=False))
        assert self.retire_report["applied"]["state"] == "applied", self.retire_report["plan"]["blockers"]
        self.c_manifest = read_manifest(str(self.project))
        self.c_project = root / "parcels-c"
        shutil.copytree(self.project, self.c_project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        self.c_ops = {c["column"]: c for c in self.c_manifest["phased_changes"]}
        self.c_tag = self.c_manifest["application_tag"]
        self.c_head = self.c_manifest["migrations"][-1]["revision"]
        self.retired_at = (self.now - dt.timedelta(hours=25)).isoformat()
        self.c_evidence_doc = _evidence(self.c_ops, state="retire", truth_new=True, head=self.c_head, tag=self.c_tag, version=self.c_ops["label"]["application_version"], retired_at=self.retired_at, now=self.now)
        self.c_evidence = self.write("ev-c.json", self.c_evidence_doc)
        self.phrase = confirmation_phrase([self.c_ops["label"], self.c_ops["weight"]])
        self.contract_doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(self.project)}, "authorization": {"contract": {"declared_by": "tests", "evidence": str(self.c_evidence)}, "destructive": {"approved_by": "tests (approver)", "old_versions_stopped": True, "min_retire_age_hours": 24}}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "contract_fields": [{"name": "label", "operation": self.c_ops["label"]["op_id"]}, {"name": "weight", "operation": self.c_ops["weight"]["op_id"]}]}]}], "invariants": NON_DESTRUCTIVE}
        self.contract_path = self.write("contract.json", self.contract_doc)

    def write(self, name: str, doc: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def blockers(self, doc: dict, name: str = "tmp.json", **kw) -> List[str]:
        plan = plan_evolution(load_evolution(str(self.write(name, doc))), **kw)
        return sorted({b.split(":")[0] for b in plan.blockers})

    def variant(self, name: str, mutate: Callable[[dict], None], doc: Optional[dict] = None, **kw) -> List[str]:
        """The blockers of the destructive contract with one condition broken in the evidence."""
        ev = copy.deepcopy(self.c_evidence_doc)
        mutate(ev)
        path = self.write(f"ev-{name}.json", ev)
        contract = copy.deepcopy(doc or self.contract_doc)
        contract["authorization"]["contract"]["evidence"] = str(path)
        kw.setdefault("authorize_contract", True)
        kw.setdefault("confirm_irreversible", self.phrase)
        return self.blockers(contract, f"contract-{name}.json", **kw)

    def read(self, project: Path, rel: str) -> str:
        return (project / rel).read_text(encoding="utf-8")


def _set_ops(ev: dict, **kw) -> None:
    for entry in ev["operations"].values():
        for key, value in kw.items():
            if isinstance(value, dict) and isinstance(entry.get(key), dict):
                entry[key].update(value)
            else:
                entry[key] = value


class ContractValidationTests(unittest.TestCase):
    def errors(self, doc: dict) -> List[str]:
        _c, errors = validate_evolution(doc, source=None)
        return errors

    def test_retire_fields_and_the_destructive_block_need_schema_14(self) -> None:
        base = {"project": {"path": "/tmp/x"}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "retire_fields": [{"name": "label", "operation": "0123456789abcdef"}]}]}]}
        for schema in ("fastapi-architect/evolution/1.2", "fastapi-architect/evolution/1.3"):
            errors = self.errors(dict(base, schema=schema))
            self.assertTrue(any("retire_fields: needs schema fastapi-architect/evolution/1.4" in e for e in errors), (schema, errors))
        self.assertFalse([e for e in self.errors(dict(base, schema=EVOLUTION_SCHEMA)) if "retire_fields" in e])
        destructive = {"schema": "fastapi-architect/evolution/1.3", "project": {"path": "/tmp/x"}, "authorization": {"contract": {"declared_by": "x"}, "destructive": {"approved_by": "x"}}, "modify_modules": [{"name": "s", "entities": [{"name": "S", "add_operations": ["list"]}]}]}
        self.assertTrue(any("authorization.destructive: needs schema fastapi-architect/evolution/1.4" in e for e in self.errors(destructive)))
        rename_14 = {"schema": EVOLUTION_SCHEMA, "project": {"path": "/tmp/x"}, "modify_modules": [{"name": "s", "entities": [{"name": "S", "rename_fields": [{"from": "a", "to": "b"}]}]}]}
        self.assertFalse([e for e in self.errors(rename_14) if "rename_fields" in e], "rename_fields is still accepted under 1.4")

    def test_the_destructive_block_is_validated_by_name(self) -> None:
        def doc(destructive: object) -> dict:
            return {"schema": EVOLUTION_SCHEMA, "project": {"path": "/tmp/x"}, "authorization": {"contract": {"declared_by": "x"}, "destructive": destructive}, "modify_modules": [{"name": "s", "entities": [{"name": "S", "add_operations": ["list"]}]}]}

        self.assertTrue(any("destructive.approved_by: required" in e for e in self.errors(doc({}))))
        self.assertTrue(any("destructive.bogus: unknown property" in e for e in self.errors(doc({"approved_by": "x", "bogus": 1}))))
        self.assertTrue(any("min_retire_age_hours: expected a number of hours, zero or more" in e for e in self.errors(doc({"approved_by": "x", "min_retire_age_hours": -1}))))
        self.assertTrue(any("old_versions_stopped: expected true or false" in e for e in self.errors(doc({"approved_by": "x", "old_versions_stopped": "yes"}))))
        self.assertTrue(any("ignore_sessions: expected a list of names" in e for e in self.errors(doc({"approved_by": "x", "ignore_sessions": "psql"}))))
        self.assertTrue(any("expected an object with approved_by" in e for e in self.errors(doc("martin"))))
        contract, errors = validate_evolution(doc({"approved_by": "x"}), source=None)
        self.assertEqual(errors, [])
        assert contract is not None
        self.assertEqual(contract.authorization["destructive"], {"approved_by": "x", "old_versions_stopped": False, "min_retire_age_hours": 24.0, "ignore_sessions": [], "reviewed_functions": []})

    def test_the_examples_validate_and_the_older_ones_still_load(self) -> None:
        for path in (RETIRE_EXAMPLE, CONTRACT_EXAMPLE):
            contract = load_evolution(str(path))
            self.assertEqual(contract.schema, "fastapi-architect/evolution/1.4", path.name)  # the increment-2 examples keep their schema
            self.assertTrue(contract.authorization["contract"]["evidence"].endswith(".sample.json"))
        self.assertEqual(load_evolution(str(CONTRACT_EXAMPLE)).authorization["destructive"]["approved_by"], "release owner, after the review of the retire evidence")
        self.assertNotIn("no_destructive_schema_change", load_evolution(str(CONTRACT_EXAMPLE)).invariants)
        self.assertEqual(load_evolution(str(RENAME_EXAMPLE)).schema, "fastapi-architect/evolution/1.3")
        for name in ("parcels-rename-evidence.sample.json", "parcels-retire-evidence.sample.json"):
            doc = json.loads((EXAMPLES / "evolutions" / name).read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "fastapi-architect/migration-evidence/1.2")
            self.assertIn("sessions", doc)

    def test_older_evidence_schemas_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for schema in ("fastapi-architect/migration-evidence/1.0", "fastapi-architect/migration-evidence/1.1", "fastapi-architect/migration-evidence/1.2"):
                path = Path(tmp) / "e.json"
                path.write_text(json.dumps({"schema": schema, "generated_at": "2026-10-01T00:00:00+00:00", "alembic": {"current": ["a"]}, "operations": {}, "indexes": []}), encoding="utf-8")
                data, errors = load_evidence(str(path))
                self.assertEqual(errors, [], schema)
                self.assertIsNotNone(data)


class RetireAndContractTests(unittest.TestCase):
    scenario: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls._root = tempfile.TemporaryDirectory(prefix="fa-retire-")
        cls.scenario = Scenario(Path(cls._root.name).resolve())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._root.cleanup()

    # -- retire ------------------------------------------------------------------ #

    def test_a_rename_of_a_field_with_a_default_is_refused_and_the_trigger_never_resolves_a_contradiction(self) -> None:
        s = self.scenario
        project = Path(tempfile.mkdtemp(prefix="fa-retire-lane-", dir=str(s.root))) / "parcels"
        shutil.copytree(s.b_project, project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        doc = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "add_fields": [{"name": "lane", "type": "str", "max_length": 10, "default": "ground"}]}]}], "invariants": INVARIANTS}
        report = run_evolution(str(s.write("add-lane.json", doc)), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        rename = {"schema": EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment", "rename_fields": [{"from": "lane", "to": "route"}]}]}], "invariants": INVARIANTS}
        self.assertIn("rename_defaulted_column_unsupported", s.blockers(rename, "rename-lane.json"))
        expand = next(s.b_project.glob("migrations/versions/*_expand_*.py")).read_text(encoding="utf-8")
        self.assertNotIn("the truth wins", expand, "no statement is ever resolved by precedence")
        self.assertEqual(expand.count("RAISE EXCEPTION USING ERRCODE = 'check_violation'"), 4, "insert and update branches of both renames raise")

    def test_retire_needs_evidence_and_an_identity_and_refuses_what_is_not_a_pending_rename(self) -> None:
        s = self.scenario
        bare = copy.deepcopy(s.retire_doc)
        bare["project"]["path"] = str(s.b_project)
        bare["authorization"] = {"contract": {"declared_by": "tests"}}
        self.assertIn("contract_evidence_valid", s.blockers(bare, "retire-no-evidence.json"))
        stale = copy.deepcopy(bare)
        ev = _evidence(s.ops, state="expand", truth_new=False, head=s.b_head, tag=s.b_tag, version=s.ops["label"]["application_version"], now=s.now - dt.timedelta(hours=30))
        stale["authorization"]["contract"]["evidence"] = str(s.write("ev-stale.json", ev))
        self.assertIn("retire_evidence_fresh", s.blockers(stale, "retire-stale.json"))
        divergent = copy.deepcopy(bare)
        ev = _evidence(s.ops, state="expand", truth_new=False, head=s.b_head, tag=s.b_tag, version=s.ops["label"]["application_version"], now=s.now)
        _set_ops(ev, mismatch_rows=3, columns_agree=False, backfill_complete=False)
        divergent["authorization"]["contract"]["evidence"] = str(s.write("ev-divergent.json", ev))
        self.assertTrue({"retire_columns_agree", "retire_backfill_complete"} <= set(s.blockers(divergent, "retire-divergent.json")))
        disabled = copy.deepcopy(bare)
        ev = _evidence(s.ops, state="expand", truth_new=False, head=s.b_head, tag=s.b_tag, version=s.ops["label"]["application_version"], now=s.now)
        _set_ops(ev, sync={"enabled": False})
        disabled["authorization"]["contract"]["evidence"] = str(s.write("ev-disabled.json", ev))
        self.assertIn("retire_sync_enabled", s.blockers(disabled, "retire-disabled.json"))
        wrong_version = copy.deepcopy(bare)
        ev = _evidence(s.ops, state="expand", truth_new=False, head=s.b_head, tag=s.b_tag, version="000000000000", now=s.now)
        wrong_version["authorization"]["contract"]["evidence"] = str(s.write("ev-version.json", ev))
        self.assertIn("retire_application_version_accredited", s.blockers(wrong_version, "retire-version.json"))
        unknown = copy.deepcopy(s.retire_doc)
        unknown["project"]["path"] = str(s.b_project)
        unknown["modify_modules"][0]["entities"][0]["retire_fields"] = [{"name": "label", "operation": "0123456789abcdef"}]
        self.assertIn("retire_operation_known", s.blockers(unknown, "retire-unknown.json"))
        again = copy.deepcopy(s.retire_doc)  # on the retired project: the operation is no longer pending
        self.assertIn("retire_operation_pending", s.blockers(again, "retire-again.json"))

    def test_the_retired_generation_knows_the_new_name_only_and_tags_its_connections(self) -> None:
        s = self.scenario
        for rel in ("src/parcels/models/shipments.py", "src/parcels/schemas/shipments.py", "src/parcels/services/shipments.py", "src/parcels/api/shipments.py", "tests/test_shipments.py"):
            text = s.read(s.c_project, rel)
            self.assertNotIn("reference", text, rel)
            self.assertNotIn("weight_kg", text, rel)
        self.assertNotIn("AliasChoices", s.read(s.c_project, "src/parcels/schemas/shipments.py"))
        self.assertNotIn("_TRANSITIONS", s.read(s.c_project, "src/parcels/services/shipments.py"))
        self.assertIn("label: Mapped[str] = mapped_column(String(40))", s.read(s.c_project, "src/parcels/models/shipments.py"))
        self.assertEqual(len({s.a_tag, s.b_tag, s.c_tag}), 3, "A, B and C announce different application names")
        self.assertIn(f'APPLICATION_NAME = "{s.c_tag}"', s.read(s.c_project, "src/parcels/core/db.py"))
        self.assertIn('connect_args=settings', s.read(s.c_project, "src/parcels/core/db.py"))
        self.assertEqual(s.c_tag, application_tag(SimpleNamespace(package="parcels"), s.c_manifest["phased_changes"]))  # type: ignore[arg-type]
        self.assertEqual(s.b_tag, application_tag(SimpleNamespace(package="parcels"), s.b_manifest["phased_changes"]))  # type: ignore[arg-type]
        self.assertEqual(s.ops["label"]["application_version"], s.c_ops["label"]["application_version"], "B and C share the contract digest: the generation tag tells them apart, not the version")
        self.assertEqual({v["state_declared"] for v in s.c_ops.values()}, {"retire"})
        self.assertEqual(s.c_ops["label"]["compatible_tags"], [s.c_tag])
        self.assertEqual((s.c_ops["label"]["source_of_truth"], s.c_ops["label"]["read_policy"], s.c_ops["label"]["write_policy"]), ("to_column", "to_column", "to_column"))
        self.assertEqual(s.c_manifest["schema"], "fastapi-architect/generation/1.6")
        self.assertEqual(s.c_manifest["history"][-1]["phased"]["retired"], sorted([s.c_ops["label"]["op_id"], s.c_ops["weight"]["op_id"]]) if s.c_ops["label"]["op_id"] < s.c_ops["weight"]["op_id"] else [s.c_ops["label"]["op_id"], s.c_ops["weight"]["op_id"]])

    def test_the_retire_revision_moves_the_truth_online_and_its_downgrade_moves_it_back(self) -> None:
        s = self.scenario
        text = next(s.c_project.glob("migrations/versions/*_retire_*.py")).read_text(encoding="utf-8")
        upgrade, downgrade = text.split("def downgrade()")
        self.assertIn("autocommit_block", upgrade)
        self.assertEqual(upgrade.split("def upgrade()")[1].count("CREATE OR REPLACE FUNCTION"), 2)
        self.assertIn('_set_not_null_online("shipments", "label")', upgrade)
        self.assertNotIn('_set_not_null_online("shipments", "weight")', upgrade, "weight_kg was nullable: nothing to carry")
        self.assertIn('_stamp("fn_shipments_', upgrade)
        self.assertIn("retired_at=", upgrade)
        self.assertIn("_require_in_sync(", upgrade)
        self.assertNotIn("DROP COLUMN", text)
        self.assertNotIn("DROP TRIGGER", text)
        self.assertIn('ALTER COLUMN "label" DROP NOT NULL', downgrade)
        self.assertIn('"reference", False)', downgrade, "the downgrade stamps the old column as the truth again")
        plan = s.retire_report["plan"]
        phases = {p["phase"]: p for p in plan["migration"]["phases"]}
        self.assertIn("retire", phases)
        self.assertEqual(phases["retire"]["reversible"], True)
        self.assertIn("B and C may run together", phases["retire"]["coexistence"])
        self.assertEqual(len(plan["migration"]["retires"]), 2)
        self.assertEqual(s.retire_report["schema"], "evolution_report/1.7")
        self.assertTrue(any(c["classification"] == "potentially_incompatible" and c["kind"] == "retire_rename" for c in plan["changes"]))
        runner_defs = s.read(s.c_project, "src/parcels/ops/backfills.py")
        self.assertIn("'state_declared': 'retire'", runner_defs)
        self.assertIn("'required': True", runner_defs)
        self.assertIn("'required': False", runner_defs)

    # -- the destructive contract ------------------------------------------------- #

    def test_the_destructive_contract_needs_both_flags_the_approver_and_the_declaration(self) -> None:
        s = self.scenario
        self.assertEqual(s.blockers(s.contract_doc, "c0.json"), ["contract_authorized", "contract_irreversible_confirmed"])
        self.assertEqual(s.blockers(s.contract_doc, "c1.json", authorize_contract=True), ["contract_irreversible_confirmed"])
        self.assertEqual(s.blockers(s.contract_doc, "c2.json", authorize_contract=True, confirm_irreversible="drop shipments.reference"), ["contract_irreversible_confirmed"])
        self.assertEqual(s.blockers(s.contract_doc, "c3.json", confirm_irreversible=s.phrase), ["contract_authorized"], "the confirmation never implies the authorization")
        self.assertEqual(s.phrase, "drop shipments.reference, shipments.weight_kg")
        plan = plan_evolution(load_evolution(str(s.contract_path)), authorize_contract=True, confirm_irreversible=s.phrase)
        self.assertEqual((plan.state, plan.blockers), ("planned", []))
        names = {p.name for p in plan.preconditions}
        listed = {name for requirement in DROP_PRECONDITIONS for name in re.findall(r"contract_[a-z_]+", requirement)}
        self.assertEqual(len(DROP_PRECONDITIONS), 16)
        self.assertTrue(listed <= names, f"preconditions listed but not evaluated: {sorted(listed - names)}")
        invariant = dict(s.contract_doc, invariants=INVARIANTS)
        self.assertEqual(s.blockers(invariant, "c4.json", authorize_contract=True, confirm_irreversible=s.phrase), ["no_destructive_schema_change"])
        undeclared = copy.deepcopy(s.contract_doc)
        undeclared["authorization"]["destructive"]["old_versions_stopped"] = False
        self.assertEqual(s.blockers(undeclared, "c5.json", authorize_contract=True, confirm_irreversible=s.phrase), ["contract_old_versions_declared_stopped"])
        no_approver = copy.deepcopy(s.contract_doc)
        no_approver["authorization"]["destructive"].pop("approved_by")
        self.assertTrue(any("destructive.approved_by: required" in e for e in validate_evolution(no_approver, source=None)[1]))

    def test_every_adversarial_condition_blocks_by_its_name(self) -> None:
        s = self.scenario
        cases = {
            "b-still-connected": (lambda ev: ev["sessions"].append({"application_name": s.b_tag, "user": "app", "count": 1, "oldest_backend_start": s.now.isoformat(), "identified": True}), {"contract_deployments_identified"}),
            "a-unidentified": (lambda ev: ev["sessions"].append({"application_name": "", "user": "app", "count": 3, "oldest_backend_start": s.now.isoformat(), "identified": False}), {"contract_deployments_identified"}),
            "psql-console": (lambda ev: ev["sessions"].append({"application_name": "psql", "user": "dba", "count": 1, "oldest_backend_start": s.now.isoformat(), "identified": False}), {"contract_deployments_identified"}),
            "divergent": (lambda ev: _set_ops(ev, mismatch_rows=2, columns_agree=False, pending_rows=2, backfill_complete=False), {"contract_backfill_complete", "contract_columns_agree"}),
            "stale": (lambda ev: ev.update(generated_at=(s.now - dt.timedelta(hours=30)).isoformat()), {"contract_evidence_fresh", "contract_evidence_after_retire"}),
            "not-retired-on-db": (lambda ev: _set_ops(ev, sync={"truth": "reference", "retired_at": None}), {"contract_retire_applied", "contract_evidence_after_retire", "contract_retire_age"}),
            "too-young": (lambda ev: _set_ops(ev, sync={"retired_at": (s.now - dt.timedelta(hours=1)).isoformat()}), {"contract_retire_age"}),
            "trigger-disabled": (lambda ev: _set_ops(ev, sync={"enabled": False}), {"contract_sync_enabled"}),
            "dependent-view": (lambda ev: _set_ops(ev, unmanaged_dependents=["rule _RETURN on view shipments_report"]), {"contract_dependencies_reviewed"}),
            "dependent-index": (lambda ev: _set_ops(ev, unmanaged_dependents=["index ix_manual_reference"]), {"contract_dependencies_reviewed"}),
            "dependent-fk": (lambda ev: _set_ops(ev, unmanaged_dependents=["constraint fk_labels_reference on table labels"]), {"contract_dependencies_reviewed"}),
            "foreign-trigger": (lambda ev: _set_ops(ev, other_triggers=[{"name": "audit_ref", "function": "audit_fn", "mentions_column": True}]), {"contract_dependencies_reviewed"}),
            "function-naming": (lambda ev: _set_ops(ev, functions_mentioning=["public.report_shipments"]), {"contract_dependencies_reviewed"}),
            "other-head": (lambda ev: ev["alembic"].update(current=["ffffffffffff"]), {"contract_database_at_head"}),
            "two-heads-db": (lambda ev: ev["alembic"].update(current=[s.c_head, "ffffffffffff"]), {"contract_database_at_head", "contract_single_head"}),
            "not-null-missing": (lambda ev: _set_ops(ev, columns={"label": {"present": True, "nullable": True, "type": "character varying", "default": None}}), {"contract_constraints_carried"}),
            "type-differs": (lambda ev: _set_ops(ev, columns={"label": {"present": True, "nullable": False, "type": "text", "default": None}}), {"contract_constraints_carried"}),
            "b-runner-wrote-it": (lambda ev: _set_ops(ev, state_declared="expand"), {"contract_retire_applied"}),
            "wrong-version": (lambda ev: ev.update(application_version_declared="000000000000"), {"contract_application_version_accredited"}),
            "evidence-1.1": (lambda ev: (ev.update(schema="fastapi-architect/migration-evidence/1.1"), ev.pop("sessions")), {"contract_deployments_identified"}),
            "other-operation": (lambda ev: ev["operations"].clear(), {"contract_evidence_matches"}),
        }
        for name, (mutate, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(set(s.variant(name, mutate)), expected)
        reviewed = copy.deepcopy(s.contract_doc)
        reviewed["authorization"]["destructive"]["reviewed_functions"] = ["public.report_shipments"]
        self.assertEqual(s.variant("function-reviewed", lambda ev: _set_ops(ev, functions_mentioning=["public.report_shipments"]), doc=reviewed), [])
        ignored = copy.deepcopy(s.contract_doc)
        ignored["authorization"]["destructive"]["ignore_sessions"] = ["psql"]
        self.assertEqual(s.variant("psql-ignored", lambda ev: ev["sessions"].append({"application_name": "psql", "user": "dba", "count": 1, "oldest_backend_start": s.now.isoformat(), "identified": False}), doc=ignored), [])
        self.assertEqual(s.variant("unmanaged-cannot-be-reviewed", lambda ev: _set_ops(ev, unmanaged_dependents=["index ix_manual_reference"]), doc=reviewed), ["contract_dependencies_reviewed"])
        zero_wait = copy.deepcopy(s.contract_doc)
        zero_wait["authorization"]["destructive"]["min_retire_age_hours"] = 0
        self.assertEqual(s.variant("zero-wait", lambda ev: _set_ops(ev, sync={"retired_at": (s.now - dt.timedelta(minutes=1)).isoformat()}), doc=zero_wait), [])

    def test_a_lock_held_by_another_process_and_a_hand_edited_file_block_the_drop(self) -> None:
        s = self.scenario
        lock = acquire(s.project, kind="test", description="another operation")
        try:
            self.assertEqual(s.blockers(s.contract_doc, "c-lock.json", authorize_contract=True, confirm_irreversible=s.phrase), ["contract_no_pending_operations"])
        finally:
            lock.release()
        edited = Path(tempfile.mkdtemp(prefix="fa-retire-edited-", dir=str(s.root))) / "parcels"
        shutil.copytree(s.c_project, edited, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        service = edited / "src/parcels/services/shipments.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# edited by hand\n", encoding="utf-8")
        doc = copy.deepcopy(s.contract_doc)
        doc["project"]["path"] = str(edited)
        blockers = s.blockers(doc, "c-edited.json", authorize_contract=True, confirm_irreversible=s.phrase)
        self.assertIn("contract_baseline_clean", blockers)

    def test_the_drop_revision_is_one_transaction_restrict_only_with_a_conditional_downgrade(self) -> None:
        s = self.scenario
        project = Path(tempfile.mkdtemp(prefix="fa-retire-drop-", dir=str(s.root))) / "parcels"
        shutil.copytree(s.c_project, project, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        doc = copy.deepcopy(s.contract_doc)
        doc["project"]["path"] = str(project)
        report = run_evolution(str(s.write("drop.json", doc)), EvolveOptions(apply=True, verify=False, authorize_contract=True, confirm_irreversible=s.phrase))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        text = next(project.glob("migrations/versions/*_drop_*.py")).read_text(encoding="utf-8")
        upgrade, downgrade = text.split("def downgrade()")
        self.assertNotIn("CASCADE", upgrade.replace("never CASCADE", ""))
        self.assertEqual(upgrade.count('DROP COLUMN "'), 2)
        self.assertIn('ALTER TABLE "shipments" DROP COLUMN "reference" RESTRICT', upgrade)
        self.assertIn("LOCK TABLE", upgrade)
        self.assertIn("lock_timeout", upgrade)
        self.assertNotIn("autocommit_block", upgrade, "one transaction: a failure anywhere rolls everything back")
        self.assertIn("DROP TRIGGER IF EXISTS trg_shipments_", upgrade)
        self.assertIn("DROP FUNCTION IF EXISTS fn_shipments_", upgrade)
        self.assertIn("pg_depend", upgrade)
        self.assertIn("_require_time_and_sessions()", upgrade.split("def upgrade()")[1])
        self.assertIn(f'COMPATIBLE_TAGS = {json.dumps(sorted({s.c_tag, read_manifest(str(project))["application_tag"]}))}', upgrade)
        self.assertIn("EVIDENCE_GENERATED_AT = '", upgrade)
        self.assertIn("MAX_EVIDENCE_AGE_HOURS = 24.0", upgrade)
        self.assertIn("MIN_RETIRE_AGE_HOURS = 24.0", upgrade)
        self.assertIn("pg_stat_activity", upgrade)
        self.assertIn("SET LOCAL lock_timeout", downgrade, "the downgrade fails fast instead of queuing behind traffic")
        self.assertIn("Approved by: tests (approver)", upgrade)
        self.assertIn(f"Confirmation phrase: {s.phrase}", upgrade)
        self.assertIn('op.add_column("shipments", sa.Column("reference", sa.String(length=40), nullable=True))', downgrade)
        self.assertIn('UPDATE "shipments" SET "reference" = "label"', downgrade)
        self.assertIn('ALTER COLUMN "reference" SET NOT NULL', downgrade)
        self.assertNotIn('ALTER COLUMN "weight_kg" SET NOT NULL', downgrade)
        self.assertIn("CREATE TRIGGER trg_shipments_", downgrade)
        self.assertIn("retired_at=", downgrade)
        manifest = read_manifest(str(project))
        ops = {c["column"]: c for c in manifest["phased_changes"]}
        self.assertEqual({v["state_declared"] for v in ops.values()}, {"contract"})
        self.assertEqual(ops["label"]["dropped"]["column"], "reference")
        self.assertEqual(ops["label"]["destructive"]["approved_by"], "tests (approver)")
        self.assertEqual(ops["label"]["destructive"]["confirmation"], s.phrase)
        plan = report["plan"]
        contract_phase = [p for p in plan["migration"]["phases"] if p["phase"] == "contract"][-1]
        self.assertEqual((contract_phase["destructive"], contract_phase["reversible"], contract_phase["transactional"]), (True, "conditional", True))
        self.assertIn("no online rollback", contract_phase["compatibility_restoration"])
        assurance = contract_phase["assurance"]
        self.assertEqual(set(assurance), {"human_declaration", "observed_sessions", "schema_state", "automated_verification", "residual_uncertainty"})
        self.assertTrue(any("intermittently" in u for u in assurance["residual_uncertainty"]))
        self.assertTrue(any("plan.migration.phases[contract].assurance" in r for r in plan["risks"]))
        self.assertTrue(all(e["destructive"] and e["reversible"] == "conditional" for e in plan["migration"]["contract"]))
        self.assertTrue(any(c["classification"] == "destructive" and c["kind"] == "drop_column" for c in plan["changes"]))
        self.assertEqual(report["contract_authorization"]["confirm_irreversible"], s.phrase)
        self.assertEqual(manifest["history"][-1]["phased"]["dropped"], [ops["label"]["op_id"], ops["weight"]["op_id"]] if plan["phased"]["dropping"][0]["column"] == "label" else [ops["weight"]["op_id"], ops["label"]["op_id"]])
        runner_defs = (project / "src/parcels/ops/backfills.py").read_text(encoding="utf-8")
        self.assertIn("'state_declared': 'contract'", runner_defs)
        backfill_tests = (project / "tests/test_backfill.py").read_text(encoding="utf-8")
        self.assertIn("RENAMES = [\n]", backfill_tests, "no trigger test once the triggers are gone")
        again = s.blockers(doc, "drop-again.json", authorize_contract=True, confirm_irreversible=s.phrase)
        self.assertIn("contract_operation_pending", again)
        self._lint(project)

    def test_the_transitional_generation_refuses_contradictory_values_at_the_boundary(self) -> None:
        s = self.scenario
        schemas = s.read(s.b_project, "src/parcels/schemas/shipments.py")
        self.assertIn("def reject_contradictory_names(", schemas)
        self.assertIn("RENAMED_SHIPMENT = (('label', 'reference'), ('weight', 'weight_kg'))", schemas)
        self.assertEqual(schemas.count("_one_value_per_renamed_field"), 2, "create and update schemas")
        tests = s.read(s.b_project, "tests/test_shipments.py")
        self.assertIn("test_create_shipment_refuses_reference_and_label_in_one_request", tests)
        backfill_tests = s.read(s.b_project, "tests/test_backfill.py")
        self.assertIn("test_the_sync_trigger_refuses_one_statement_that_sets_both_names_to_different_values", backfill_tests)
        expand = next(s.b_project.glob("migrations/versions/*_expand_*.py")).read_text(encoding="utf-8")
        self.assertIn("RAISE EXCEPTION USING ERRCODE = 'check_violation'", expand)
        self.assertIn("COMMENT ON FUNCTION fn_shipments_", expand)
        self.assertIn("truth=reference", expand)
        self.assertNotIn("RENAME COLUMN", expand)
        self.assertIn("B and C may run together", json.dumps(s.retire_report["plan"]["migration"]["phases"]))
        self._lint(s.b_project)
        self._lint(s.c_project)

    def _lint(self, project: Path) -> None:
        if shutil.which("uvx") is None:
            self.skipTest("uvx is not installed")
        proc = subprocess.run(RUFF, cwd=str(project), capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-1000:])
        compiled = subprocess.run([sys.executable, "-m", "compileall", "-q", "src", "tests", "migrations"], cwd=str(project), capture_output=True, text=True, timeout=300)
        self.assertEqual(compiled.returncode, 0, compiled.stdout[-2000:] + compiled.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
