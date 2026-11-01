"""The opt-in three-way merge wired into evolutions (v2.2, increment 3), without a database.

Scenarios on copies of the committed ``team-notes`` snapshot (in memory): A, an independent manual change in
``main.py`` preserved across an evolution that regenerates the file; B, concurrent changes at the same insertion
point refused; C, manual keys in ``.audit-fastapi.json`` preserved (and the three-way JSON merge on synthetic
documents); D, Python with comments, decorators, imports and a complex signature; E, a manual change inside a region
the contract declares generator-owned, refused. One evolution runs the whole way (plan, verification in a copy with
the generated suite under the trust model, apply, repeat as a no-op). The adversarial controls: BASE absent, corrupt
or from another contract; LOCAL changed after the plan; NEW changed after the verification; no authorisation (either
half); too broad an authorisation; signature and decorator conflicts; duplicate import; duplicate route; comments;
CRLF and tabs; symlink; interrupted journal, recovery and retry; a lock held by another process; idempotent repetition.
Every refusal leaves the project's files byte-identical.
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
from typing import Dict, List, Optional

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import DEFAULT_WALK_SKIP  # noqa: E402
from fastapi_design import journal  # noqa: E402
from fastapi_design.evolution import EVOLUTION_SCHEMA, EvolveOptions, _write_plan, load_evolution, plan_evolution, run_evolution, validate_evolution  # noqa: E402
from fastapi_design.generator import WriteError, read_manifest  # noqa: E402
from fastapi_design.locking import acquire  # noqa: E402
from fastapi_design.merging import plan_merge, validate_merge_authorizations  # noqa: E402

EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
TEAM_NOTES = EXAMPLES / "generated" / "team-notes"
MAIN = "src/team_notes/main.py"
ROUTER = "src/team_notes/api/router.py"
AUDIT = ".audit-fastapi.json"
INVARIANTS = ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"]
PY_MERGE = {"path": MAIN, "transform": "three_way_python"}
HAS_FASTAPI = all(importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx", "pytest"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digests(project: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in project.rglob("*"):
        if p.is_file() and not any(part in DEFAULT_WALK_SKIP for part in p.parts):
            out[p.relative_to(project).as_posix()] = _sha(p)
    return out


class MergeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-merge-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, name: str = "project") -> Path:
        target = self.tmp / name
        shutil.copytree(TEAM_NOTES, target, ignore=shutil.ignore_patterns(*DEFAULT_WALK_SKIP))
        return target

    def contract(self, project: Path, merge: Optional[List[dict]] = None, *, name: str = "evolution.json", module: str = "reminders", entity: str = "Reminder", schema: str = EVOLUTION_SCHEMA, files_extra: Optional[dict] = None) -> Path:
        doc = {"schema": schema, "project": {"path": str(project)}, "add_modules": [{"name": module, "description": module, "entities": [{"name": entity, "fields": [{"name": "text", "type": "str", "max_length": 80}], "operations": ["create", "get", "list"]}]}], "invariants": INVARIANTS}
        files = dict(files_extra or {})
        if merge is not None:
            files["merge"] = merge
        if files:
            doc["files"] = files
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def plan(self, contract_path: Path, **kw):
        return plan_evolution(load_evolution(str(contract_path)), **kw)

    def blockers(self, plan) -> List[str]:
        return sorted({b.split(":")[0] for b in plan.blockers})

    def manual_main(self, project: Path) -> str:
        """Scenario A's edit: an import, a middleware line inside create_app, a decorated helper with comments."""
        main = project / MAIN
        text = main.read_text(encoding="utf-8")
        text = text.replace("from fastapi import FastAPI\n", "from fastapi import FastAPI\nfrom starlette.middleware.gzip import GZipMiddleware  # manual: compress large payloads\n")
        text = text.replace("    install_error_handlers(app)\n", "    install_error_handlers(app)\n    app.add_middleware(GZipMiddleware, minimum_size=1024)  # manual: added by the team\n")
        text = text.replace("\n\napp = create_app()\n", '''

def request_id_header(*, header: str = "x-request-id", generator=None) -> dict[str, str]:  # manual helper
    """Manual: a header dict the ops team reuses in scripts."""
    return {header: (generator or (lambda: "fixed"))()}


app = create_app()
''')
        main.write_text(text, encoding="utf-8")
        return text


class AuthorizationTests(MergeCase):
    def test_merge_needs_schema_15_and_exact_well_formed_entries(self) -> None:
        for schema in ("fastapi-architect/evolution/1.3", "fastapi-architect/evolution/1.4"):
            _c, errors = validate_evolution(json.loads(self.contract(self.tmp, [PY_MERGE], schema=schema).read_text()), source=None)
            self.assertTrue(any("files.merge: needs schema fastapi-architect/evolution/1.5" in e for e in errors), (schema, errors))
        cases = {
            "glob": ([{"path": "src/team_notes/*.py", "transform": "three_way_python"}], "is a pattern or a directory"),
            "directory": ([{"path": "src/team_notes/", "transform": "three_way_python"}], "is a pattern or a directory"),
            "escape": ([{"path": "../other/main.py", "transform": "three_way_python"}], "must stay inside the project"),
            "manifest": ([{"path": ".fastapi-architect/generation.json", "transform": "three_way_json"}], "never merged"),
            "historical revision": ([{"path": "migrations/versions/0001_x.py", "transform": "three_way_python"}], "never merged"),
            "duplicate": ([PY_MERGE, PY_MERGE], "appears twice"),
            "transform": ([{"path": MAIN, "transform": "semantic"}], "expected one of three_way_python, three_way_json"),
            "transform/extension": ([{"path": MAIN, "transform": "three_way_json"}], "applies to .json files"),
            "on_conflict": ([{"path": MAIN, "transform": "three_way_python", "on_conflict": "keep_manual"}], "only 'reject' is implemented"),
            "ownership shape": ([{"path": MAIN, "transform": "three_way_python", "ownership": "file"}], "expected an object"),
            "ownership key": ([{"path": MAIN, "transform": "three_way_python", "ownership": {"everything": True}}], "unknown property"),
            "empty may-change": ([{"path": MAIN, "transform": "three_way_python", "ownership": {"generator_may_change": []}}], "'file' or a non-empty list of regions"),
            "region shape": ([{"path": MAIN, "transform": "three_way_python", "ownership": {"developer_owns": ["create_app"]}}], "expected one of"),
            "region lines": ([{"path": MAIN, "transform": "three_way_python", "ownership": {"developer_owns": [{"lines": [9, 3]}]}}], "1 <= first <= last"),
            "json with regions": ([{"path": AUDIT, "transform": "three_way_json", "ownership": {"developer_owns": [{"lines": [1, 2]}]}}], "regions apply to Python files"),
            "unknown key": ([{"path": MAIN, "transform": "three_way_python", "resolve": "ours"}], "unknown property"),
        }
        for name, (merge, fragment) in cases.items():
            with self.subTest(name):
                items, errors = validate_merge_authorizations(merge)
                self.assertEqual(len(items), 1 if name == "duplicate" else 0, name)
                self.assertTrue(any(fragment in e for e in errors), (name, errors))
        items, errors = validate_merge_authorizations([PY_MERGE], preserve=["src/team_notes/main.py"])
        self.assertTrue(any("also listed under files.preserve" in e for e in errors))
        items, errors = validate_merge_authorizations([{"path": MAIN, "transform": "three_way_python", "ownership": {"generator_may_change": [{"function": "create_app"}], "developer_owns": [{"lines": [1, 12]}], "generator_owns": [{"class": "Nothing"}]}}])
        self.assertEqual(errors, [])
        self.assertEqual(items[0]["ownership"], {"generator_may_change": [{"function": "create_app"}], "developer_owns": [{"lines": [1, 12]}], "generator_owns": [{"class": "Nothing"}]})
        self.assertEqual(items[0]["on_conflict"], "reject")

    def test_the_default_is_still_refusal_and_both_halves_of_the_opt_in_are_required(self) -> None:
        project = self.copy()
        self.manual_main(project)
        before = _tree_digests(project)
        self.assertEqual(self.blockers(self.plan(self.contract(project))), ["manual_change_on_integration_point"])
        self.assertEqual(self.blockers(self.plan(self.contract(project, [PY_MERGE]))), ["merge_not_authorized_on_command_line"])
        self.assertEqual(self.blockers(self.plan(self.contract(project), allow_merge=True)), ["manual_change_on_integration_point"], "the flag alone merges nothing")
        report = run_evolution(str(self.contract(project, [PY_MERGE])), EvolveOptions(apply=True, verify=False))
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(_tree_digests(project), before, "a refused plan touches nothing")
        self.assertEqual(report["contract_authorization"]["allow_merge"], False)


class ScenarioTests(MergeCase):
    def test_a_an_independent_manual_change_is_preserved_and_the_evolution_repeats_as_a_noop(self) -> None:
        project = self.copy()
        local_text = self.manual_main(project)
        contract = self.contract(project, [PY_MERGE])
        plan = self.plan(contract, allow_merge=True)
        self.assertEqual((plan.state, plan.blockers, plan.merged), ("planned", [], [MAIN]))
        record = plan.merges[0]
        self.assertTrue(record["clean"])
        self.assertTrue(record["base"]["authenticated"])
        self.assertEqual(record["base"]["sha256_recorded"], read_manifest(str(project))["files"][MAIN])
        self.assertEqual(record["local_sha256"], hashlib.sha256(local_text.encode()).hexdigest())
        self.assertEqual(record["hunks"], {"generated": 2, "manual": 3})
        self.assertEqual(len(record["preserved_manual_hunks"]), 3)
        self.assertEqual(len(record["applied_generated_hunks"]), 2)
        self.assertIn("+++ merged/" + MAIN, record["diff_local_to_result"])
        self.assertEqual(record["authorization_required"], ["files.merge entry for this exact path (contract)", "--allow-merge on the command line"])
        self.assertIn("syntax and AST", record["validations_in_copy"])
        merged = plan.files[MAIN]
        for fragment in ("from starlette.middleware.gzip import GZipMiddleware  # manual: compress large payloads", "app.add_middleware(GZipMiddleware, minimum_size=1024)  # manual: added by the team", "def request_id_header(*, header: str = \"x-request-id\", generator=None) -> dict[str, str]:  # manual helper", '"""Manual: a header dict the ops team reuses in scripts."""', "InMemoryReminderRepository()"):
            self.assertIn(fragment, merged)
        self.assertEqual(hashlib.sha256(merged.encode()).hexdigest(), record["result_sha256"])
        self.assertNotIn("<<<<<<<", merged)
        self.assertEqual(plan.to_dict()["files"]["merged"], [MAIN])
        # dry-run: planning wrote nothing
        self.assertEqual((project / MAIN).read_text(encoding="utf-8"), local_text)
        report = run_evolution(str(contract), EvolveOptions(apply=True, verify=False, allow_merge=True))
        self.assertEqual(report["applied"]["state"], "applied", report["plan"]["blockers"])
        self.assertEqual((project / MAIN).read_text(encoding="utf-8"), merged)
        manifest = read_manifest(str(project))
        self.assertEqual(manifest["history"][-1]["merged"], [MAIN])
        self.assertEqual(manifest["schema"], "fastapi-architect/generation/1.6")
        self.assertNotEqual(manifest["files"][MAIN], record["result_sha256"], "the manifest records the generated content; the manual hunks stay the developer's")
        copy_for_steps = self.copy("steps")
        self.manual_main(copy_for_steps)
        op = _write_plan(copy_for_steps, self.plan(self.contract(copy_for_steps, [PY_MERGE], name="steps.json"), allow_merge=True))
        merge_step = next(s for s in op.steps if s["kind"] == "merge")
        self.assertEqual(merge_step["paths"], [MAIN])
        self.assertEqual(merge_step["evidence"][MAIN]["result_sha256"], record["result_sha256"])
        regenerate_step = next(s for s in op.steps if s["kind"] == "regenerate")
        self.assertNotIn(MAIN, regenerate_step["paths"])
        again = self.plan(self.contract(project, [PY_MERGE], name="again.json"), allow_merge=True)
        self.assertEqual(again.state, "noop")
        # a later evolution merges the same manual hunks again against the new generated base
        later = self.plan(self.contract(project, [PY_MERGE], name="later.json", module="alerts", entity="Alert"), allow_merge=True)
        self.assertEqual((later.state, later.merged), ("planned", [MAIN]), later.blockers)
        self.assertIn("def request_id_header", later.files[MAIN])
        self.assertIn("InMemoryAlertRepository()", later.files[MAIN])

    def test_b_concurrent_changes_at_the_same_insertion_point_are_a_conflict(self) -> None:
        project = self.copy()
        router = project / ROUTER
        router.write_text(router.read_text(encoding="utf-8") + 'api_router.include_router(notes_router, prefix="/legacy")  # manual, where the generator appends too\n', encoding="utf-8")
        before = _tree_digests(project)
        plan = self.plan(self.contract(project, [{"path": ROUTER, "transform": "three_way_python"}]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["insertions_at_the_same_point"])
        conflict = next(c for c in plan.merges[0]["conflicts"] if c["kind"] == "insertions_at_the_same_point")
        self.assertEqual(conflict["path"], ROUTER)
        self.assertTrue(conflict["generated"] and conflict["manual"], "both sides' text is shown")
        self.assertTrue(any("reminders" in line for line in conflict["generated"]))
        self.assertTrue(any("legacy" in line for line in conflict["manual"]))
        self.assertEqual(_tree_digests(project), before)
        self.assertFalse(any("<<<<<<<" in p.read_text(encoding="utf-8", errors="replace") for p in project.rglob("*.py")))

    def test_c_manual_keys_in_the_audit_configuration_survive_and_json_merges_three_ways(self) -> None:
        project = self.copy()
        audit = project / AUDIT
        doc = json.loads(audit.read_text(encoding="utf-8"))
        doc["x-team"] = {"owner": "ops"}
        audit.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        plan = self.plan(self.contract(project, [{"path": AUDIT, "transform": "three_way_json"}]), allow_merge=True)
        self.assertEqual((plan.state, plan.merged), ("planned", []), "the generator does not change the document in this evolution: the manual keys are simply preserved")
        self.assertIn(AUDIT, plan.preserved_manual)
        base = json.dumps({"contracts": {"route_auth_required": ["/api/*"]}, "public_routes": ["/health"], "tenant_params": []}, indent=2) + "\n"
        new = json.dumps({"contracts": {"route_auth_required": ["/api/*"], "mutation_authorization_required": ["/api/*"]}, "public_routes": ["/health"], "tenant_params": []}, indent=2) + "\n"
        local = json.dumps({"contracts": {"route_auth_required": ["/api/*"]}, "public_routes": ["/health"], "tenant_params": [], "x-team": {"owner": "ops"}}, indent=2) + "\n"
        recorded = hashlib.sha256(base.encode()).hexdigest()
        auth = {"path": AUDIT, "transform": "three_way_json", "on_conflict": "reject", "ownership": {"generator_may_change": "file", "developer_owns": [], "generator_owns": []}}
        outcome = plan_merge(AUDIT, base=base, recorded_base_digest=recorded, local=local, new=new, authorization=auth, tool_version="t")
        self.assertTrue(outcome.clean, outcome.conflicts)
        merged = json.loads(outcome.text or "{}")
        self.assertEqual((merged["x-team"], merged["contracts"]["mutation_authorization_required"]), ({"owner": "ops"}, ["/api/*"]))
        clash = json.dumps({"contracts": {"route_auth_required": ["/api/*"]}, "public_routes": ["/health", "/metrics"], "tenant_params": []}, indent=2) + "\n"
        new_clash = json.dumps({"contracts": {"route_auth_required": ["/api/*"]}, "public_routes": ["/health", "/ready"], "tenant_params": []}, indent=2) + "\n"
        outcome = plan_merge(AUDIT, base=base, recorded_base_digest=recorded, local=clash, new=new_clash, authorization=auth, tool_version="t")
        self.assertFalse(outcome.clean)
        self.assertEqual([c["kind"] for c in outcome.conflicts], ["json_value_conflict"])
        self.assertIn("public_routes", outcome.conflicts[0]["detail"])

    def test_d_comments_decorators_imports_and_a_complex_signature_survive_the_merge(self) -> None:
        project = self.copy()
        main = project / MAIN
        text = main.read_text(encoding="utf-8")
        text = text.replace("from contextlib import asynccontextmanager\n", "from contextlib import asynccontextmanager\nfrom functools import lru_cache  # manual\n")
        text = text.replace("\n\napp = create_app()\n", '''

@lru_cache(maxsize=1)  # manual: computed once
def build_info(source: str = "env", /, *, verbose: bool = False, **extra: str) -> dict[str, str | None]:
    """Manual: build metadata.

    The first comment of the body.
    """
    # a comment inside the body
    return {"source": source, **extra}  # trailing comment


app = create_app()
''')
        main.write_text(text, encoding="utf-8")
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual((plan.state, plan.merged), ("planned", [MAIN]), plan.blockers)
        merged = plan.files[MAIN]
        for fragment in ("from functools import lru_cache  # manual", "@lru_cache(maxsize=1)  # manual: computed once", "def build_info(source: str = \"env\", /, *, verbose: bool = False, **extra: str) -> dict[str, str | None]:", "    # a comment inside the body", '    return {"source": source, **extra}  # trailing comment', "InMemoryReminderRepository()"):
            self.assertIn(fragment, merged)
        compile(merged, MAIN, "exec")

    def test_e_a_manual_change_inside_a_generator_owned_region_is_blocked(self) -> None:
        project = self.copy()
        main = project / MAIN
        main.write_text(main.read_text(encoding="utf-8").replace("    app = FastAPI(title=settings.app_name, lifespan=lifespan)\n", "    app = FastAPI(title=settings.app_name, lifespan=lifespan, debug=True)  # manual, inside create_app\n"), encoding="utf-8")
        before = _tree_digests(project)
        owned = [{"path": MAIN, "transform": "three_way_python", "ownership": {"generator_owns": [{"function": "create_app"}]}}]
        plan = self.plan(self.contract(project, owned), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["manual_change_in_generator_region"])
        conflict = plan.merges[0]["conflicts"][0]
        self.assertIn("function create_app", conflict["detail"])
        self.assertTrue(conflict["manual"])
        self.assertEqual(_tree_digests(project), before)
        developer = [{"path": MAIN, "transform": "three_way_python", "ownership": {"developer_owns": [{"function": "create_app"}]}}]
        plan = self.plan(self.contract(project, developer), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["generated_change_in_developer_region"], "the generator changes create_app (the lifespan), which the developer owns")
        narrow = [{"path": MAIN, "transform": "three_way_python", "ownership": {"generator_may_change": [{"lines": [1, 12]}]}}]
        plan = self.plan(self.contract(project, narrow), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["generated_change_outside_authorized_region"])
        unknown = [{"path": MAIN, "transform": "three_way_python", "ownership": {"generator_owns": [{"function": "nowhere"}]}}]
        plan = self.plan(self.contract(project, unknown), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["merge_region_unresolved"])
        shared = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual((shared.state, shared.merged), ("planned", [MAIN]), shared.blockers)

    @unittest.skipUnless(HAS_FASTAPI, "fastapi, httpx and pytest are not importable by this interpreter")
    def test_the_whole_flow_plan_verify_apply_repeat_with_the_generated_suite(self) -> None:
        project = self.copy()
        self.manual_main(project)
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        authorization = Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: merge integration, fixture copied by this suite")
        policy = ExecutionPolicy(authorized=True, command=command, timeout_s=600)
        options = EvolveOptions(apply=True, verify=True, authorization=authorization, policy=policy, test_command=command, allow_merge=True)
        report = run_evolution(str(self.contract(project, [PY_MERGE])), options)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons") or report["plan"]["blockers"])
        verification = report["verification"]
        self.assertEqual(verification["merges"][0]["ok"], True, verification["merges"])
        self.assertEqual(verification["merges"][0]["manual_hunks"], {"expected": 3, "lost": []})
        self.assertTrue(verification["merges"][0]["parses"])
        self.assertEqual(verification["tests"]["run"]["counts"]["failed"], 0)
        self.assertEqual(verification["audit"]["blocking_new"], [])
        self.assertEqual(report["applied"]["state"], "applied")
        self.assertIn("app.add_middleware(GZipMiddleware, minimum_size=1024)", (project / MAIN).read_text(encoding="utf-8"))
        again = run_evolution(str(self.contract(project, [PY_MERGE], name="again.json")), options)
        self.assertEqual(again["state"], "noop")


class AdversarialTests(MergeCase):
    def test_base_absent_corrupt_or_from_another_contract_refuses_the_merge(self) -> None:
        project = self.copy()
        self.manual_main(project)
        manifest_path = project / ".fastapi-architect" / "generation.json"
        pristine = manifest_path.read_text(encoding="utf-8")
        # absent: the manifest does not know the file
        doc = json.loads(pristine)
        doc["files"].pop(MAIN)
        manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assertEqual(self.blockers(self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)), ["merge_base_unknown"])
        # corrupt: the recorded digest is not the digest of what the contract regenerates
        doc = json.loads(pristine)
        doc["files"][MAIN] = "0" * 64
        manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["merge_base_not_reconstructible"])
        self.assertFalse(plan.merges[0]["base"]["authenticated"])
        self.assertEqual(plan.merges[0]["base"]["sha256_recorded"], "0" * 64)
        # another contract: the manifest's spec document is not the one the file came from
        doc = json.loads(pristine)
        doc["spec_document"]["name"] = "other-notes"
        manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["baseline_contract_available"], "a manifest whose contract is not the one the digest names is refused before any merge")
        manifest_path.write_text(pristine, encoding="utf-8")
        self.assertEqual(self.plan(self.contract(project, [PY_MERGE]), allow_merge=True).state, "planned")

    def test_local_changed_after_the_plan_and_new_changed_after_the_verification_are_refused_by_the_writer(self) -> None:
        project = self.copy()
        self.manual_main(project)
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(plan.state, "planned")
        main = project / MAIN
        before = _tree_digests(project)
        main.write_text(main.read_text(encoding="utf-8") + "# edited after the plan\n", encoding="utf-8")
        with self.assertRaises(WriteError) as caught:
            _write_plan(project, plan)
        self.assertIn("changed after the plan was computed", str(caught.exception))
        after = _tree_digests(project)
        self.assertEqual({k: v for k, v in after.items() if k != MAIN}, {k: v for k, v in before.items() if k != MAIN}, "nothing else was written")
        self.assertFalse(list((project / ".fastapi-architect" / "ops").glob("*/journal.json")) if (project / ".fastapi-architect" / "ops").exists() else False)
        main.write_text(main.read_text(encoding="utf-8").replace("# edited after the plan\n", ""), encoding="utf-8")
        plan.files[MAIN] = plan.files[MAIN] + "# tampered after verification\n"
        with self.assertRaises(WriteError) as caught:
            _write_plan(project, plan)
        self.assertIn("merge result of", str(caught.exception))
        self.assertEqual(_tree_digests(project), before)

    def test_signature_decorator_import_and_route_conflicts_are_named(self) -> None:
        base = "import os\n\n\ndef f(a):\n    return a\n\n\n@app.get('/x')\ndef g():\n    return 1\n"
        auth = {"path": "m.py", "transform": "three_way_python", "on_conflict": "reject", "ownership": {"generator_may_change": "file", "developer_owns": [], "generator_owns": []}}
        recorded = hashlib.sha256(base.encode()).hexdigest()

        def outcome(new: str, local: str):
            return plan_merge("m.py", base=base, recorded_base_digest=recorded, local=local, new=new, authorization=auth, tool_version="t")

        def kinds(o) -> set:
            self.assertFalse(o.clean)
            return {c["kind"] for c in o.conflicts}

        same_line = {"overlapping_changes", "insertions_at_the_same_point"}  # the textual merge sees these first when the edits share lines
        self.assertTrue(kinds(outcome(base.replace("def f(a):", "def f(a, b=1):"), base.replace("def f(a):", "def f(a, *, c=None):"))) & ({"signature_or_decorators_changed_by_both_sides"} | same_line))
        self.assertTrue(kinds(outcome(base.replace("@app.get('/x')\n", "@app.get('/x')\n@cache\n"), base.replace("@app.get('/x')\n", "@app.get('/x')\n@retry\n"))) & ({"signature_or_decorators_changed_by_both_sides"} | same_line))
        # the generator adds a decorator, the developer changes the signature of the same function: distinct lines, same header
        self.assertIn("signature_or_decorators_changed_by_both_sides", kinds(outcome(base.replace("@app.get('/x')\n", "@app.get('/x')\n@cache\n"), base.replace("def g():", "def g(flag=False):"))))
        self.assertTrue(kinds(outcome(base.replace("import os\n", "import os\nimport json\n"), base.replace("    return 1\n", "    return 1\n\n\nimport json\n"))) & {"duplicate_addition", "duplicate_import"})
        self.assertIn("conflicting_import", kinds(outcome(base.replace("import os\n", "import os\nfrom a import X\n"), base.replace("    return 1\n", "    return 1\n\n\nfrom b import X\n"))))
        mount = "from fastapi import FastAPI\n\napp = FastAPI()\n\n\ndef wire(r):\n    app.include_router(r)\n\n\nREADY = True\n"
        rec2 = hashlib.sha256(mount.encode()).hexdigest()
        o = plan_merge("m.py", base=mount, recorded_base_digest=rec2, local=mount + "app.include_router(r2)\n", new=mount.replace("    app.include_router(r)\n", "    app.include_router(r)\n    app.include_router(r2)\n"), authorization=auth, tool_version="t")
        self.assertIn("duplicate_router_registration", [c["kind"] for c in o.conflicts])
        o = plan_merge("m.py", base=mount, recorded_base_digest=rec2, local=mount + 'app.include_router(r2, prefix="/v2")\n', new=mount.replace("    app.include_router(r)\n", "    app.include_router(r)\n    app.include_router(r2)\n"), authorization=auth, tool_version="t")
        self.assertTrue(o.clean, [c["kind"] for c in o.conflicts])

    def test_line_endings_tabs_and_lost_comments_are_refused(self) -> None:
        project = self.copy()
        main = project / MAIN
        original = main.read_text(encoding="utf-8")
        main.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["line_endings_changed"])
        main.write_text(original.replace("    install_error_handlers(app)\n", "    install_error_handlers(app)\n\tapp.state.flag = True  # manual, tab-indented\n"), encoding="utf-8")
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["local_does_not_parse"])
        main.write_text(original.replace("    install_error_handlers(app)\n", "    install_error_handlers(app)  # manual:\tthis comment holds a tab\n"), encoding="utf-8")
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual((plan.state, plan.merged), ("planned", [MAIN]), plan.blockers)
        self.assertIn("# manual:\tthis comment holds a tab", plan.files[MAIN])
        base = "a = 1\n# keep me\nb = 2\n"
        recorded = hashlib.sha256(base.encode()).hexdigest()
        auth = {"path": "m.py", "transform": "three_way_python", "on_conflict": "reject", "ownership": {"generator_may_change": "file", "developer_owns": [], "generator_owns": []}}
        outcome = plan_merge("m.py", base=base, recorded_base_digest=recorded, local="a = 1\n# keep me\n# and me\nb = 2\n", new="a = 1\nb = 2\n", authorization=auth, tool_version="t")
        self.assertFalse(outcome.clean)
        self.assertTrue({"manual_comment_lost", "overlapping_changes"} & {c["kind"] for c in outcome.conflicts}, [c["kind"] for c in outcome.conflicts])
        outcome = plan_merge("m.py", base=base, recorded_base_digest=recorded, local="a = 1\n# keep me\nb = 2\n# mine\n", new="a = 1\n# keep me\nb = 3\n", authorization=auth, tool_version="t")
        self.assertTrue(outcome.clean, outcome.conflicts)
        self.assertIn("# mine", outcome.text or "")

    def test_symlink_renamed_file_lock_interrupted_journal_recovery_and_retry(self) -> None:
        project = self.copy()
        self.manual_main(project)
        main = project / MAIN
        real = project / "src" / "team_notes" / "main_real.py"
        shutil.move(str(main), str(real))
        os.symlink(real, main)
        plan = self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["unsafe_path"])
        main.unlink()
        shutil.move(str(real), str(main))
        renamed = [{"path": "src/team_notes/main_old.py", "transform": "three_way_python"}]
        plan = self.plan(self.contract(project, renamed), allow_merge=True)
        self.assertEqual(self.blockers(plan), ["manual_change_on_integration_point"], "an authorisation for a path that is not the modified file changes nothing")
        lock = acquire(project, kind="test", description="another process")
        try:
            report = run_evolution(str(self.contract(project, [PY_MERGE])), EvolveOptions(apply=True, verify=False, allow_merge=True))
            self.assertEqual(report["applied"]["state"], "locked")
        finally:
            lock.release()
        before = _tree_digests(project)
        faults = journal.FaultInjector(points=["after_publish:1"])
        report = run_evolution(str(self.contract(project, [PY_MERGE])), EvolveOptions(apply=True, verify=False, allow_merge=True, faults=faults))
        self.assertEqual(report["applied"]["state"], "interrupted", report["applied"])
        pending = journal.pending_operations(project)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.blockers(self.plan(self.contract(project, [PY_MERGE]), allow_merge=True)), ["no_pending_operation"])
        outcome = journal.recover(project, pending[0], strategy="rollback")
        self.assertEqual(outcome.state, "rolled_back")
        self.assertEqual(_tree_digests(project), before, "the rollback restores every file, the developer's version included")
        retry = run_evolution(str(self.contract(project, [PY_MERGE])), EvolveOptions(apply=True, verify=False, allow_merge=True))
        self.assertEqual(retry["applied"]["state"], "applied", retry["plan"]["blockers"])
        self.assertIn("InMemoryReminderRepository()", (project / MAIN).read_text(encoding="utf-8"))
        self.assertIn("def request_id_header", (project / MAIN).read_text(encoding="utf-8"))
        self.assertEqual(self.plan(self.contract(project, [PY_MERGE], name="again.json"), allow_merge=True).state, "noop")

    def test_the_command_line_carries_the_flag_and_plans_without_writing(self) -> None:
        project = self.copy()
        self.manual_main(project)
        before = _tree_digests(project)
        cli = SKILL_SCRIPTS / "evolve_fastapi.py"
        proc = subprocess.run([sys.executable, str(cli), "plan", str(self.contract(project, [PY_MERGE])), "--no-verify", "--format", "json"], capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT))
        report = json.loads(proc.stdout)
        self.assertEqual(report["state"], "blocked")
        self.assertTrue(any(b.startswith("merge_not_authorized_on_command_line") for b in report["plan"]["blockers"]))
        proc = subprocess.run([sys.executable, str(cli), "plan", str(self.contract(project, [PY_MERGE])), "--no-verify", "--allow-merge", "--format", "json"], capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT))
        report = json.loads(proc.stdout)
        self.assertEqual((report["state"], report["plan"]["files"]["merged"]), ("planned", [MAIN]))
        self.assertEqual(report["contract_authorization"]["allow_merge"], True)
        self.assertEqual(_tree_digests(project), before, "plan is a dry run")
        text = subprocess.run([sys.executable, str(cli), "plan", str(self.contract(project, [PY_MERGE])), "--no-verify", "--allow-merge"], capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT)).stdout
        self.assertIn("merge", text)


if __name__ == "__main__":
    unittest.main()
