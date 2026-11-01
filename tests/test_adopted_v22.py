"""Increment 4 of v2.2: advanced adoption of hand-written projects. Adoption contract 1.1 (the exact boundary the
operator approves: package, unmanaged paths, the mount point, the conventions of new modules, the allowed
dependencies, the allowed operations, the Alembic facts), self-contained modules generated at the declared mount
point (one import and one ``include_router`` statement after the last existing registration; every other byte
unchanged), bounded extensions of adopted routers (typed parameters, allowed dependencies, delegation to a resolved
project callable, a response model of the adopted module), generated modules that evolve additively, the journal
steps that go with them, and the adversarial controls. Everything persistence-related runs on a real cluster in
``test_adopted_v22_postgresql.py`` (opt-in).

Fixtures: ``tests/fixtures/adoption22/workshop`` (application factory, services, dependencies) and
``tests/fixtures/adoption20/handmade_shop`` (module-level application). Neither is ever modified: every test copies.
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
from typing import Dict, List

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import walk_files  # noqa: E402
from fastapi_design import adopted, adoption, evolution, journal  # noqa: E402
from fastapi_design.discovery import discover  # noqa: E402
from fastapi_design.locking import acquire  # noqa: E402

WORKSHOP = FIXTURES / "adoption22" / "workshop"
HANDMADE = FIXTURES / "adoption20" / "handmade_shop"
NOTES_PG = FIXTURES / "adoption22" / "notes_pg"
CLI = SKILL_SCRIPTS / "evolve_fastapi.py"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx"))
SCHEMA = evolution.EVOLUTION_SCHEMA
A11 = adoption.ADOPTION_SCHEMA_11
A10 = adoption.ADOPTION_SCHEMA
MAIN = "app/main.py"
ITEMS = "app/routers/items.py"
USERS = "app/routers/users.py"

ITEMS_MODULE = {"name": "items", "router": {"file": ITEMS, "name": "router"}, "files": [ITEMS, "app/schemas.py"]}
CONVENTIONS = {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": "app.main", "attribute": "app"}}
WORKSHOP_MOUNT = {"file": MAIN, "function": "create_app", "app": "application", "after_call": "application.include_router", "prefix": "/api"}
HANDMADE_MOUNT = {"file": MAIN, "function": None, "app": "app", "after_call": "app.include_router", "prefix": "/api"}
ALLOWED = ["app.deps:require_api_key", "app.services:count_items", "app.services:catalogue_value"]
OPERATIONS = ["add_module", "mount_router", "add_endpoint", "add_field"]

REVIEWS = {"name": "reviews", "description": "Customer reviews", "entities": [{"name": "Review", "fields": [{"name": "author", "type": "str", "max_length": 80}, {"name": "rating", "type": "int", "min_value": 1}, {"name": "comment", "type": "str", "required": False}], "operations": ["create", "get", "list"]}]}
SUPPLIERS = {"name": "suppliers", "entities": [{"name": "Supplier", "fields": [{"name": "name", "type": "str", "max_length": 80}, {"name": "email", "type": "str", "required": False}], "operations": ["create", "get"]}]}
EXTENDED = [
    {"method": "GET", "path": "/reports/stats", "name": "items_stats", "summary": "Items at or above a price", "parameters": [{"name": "min_price", "in": "query", "type": "float", "required": False, "default": 0.0}], "delegate": {"callable": "app.services:count_items", "arguments": ["min_price"]}, "response_model": "ItemStats"},
    {"method": "GET", "path": "/reports/value", "name": "items_value", "summary": "Catalogue value", "dependencies": ["app.deps:require_api_key"], "delegate": {"callable": "app.services:catalogue_value", "arguments": []}},
    {"method": "GET", "path": "/ping/{tag}", "name": "items_ping", "parameters": [{"name": "tag", "in": "path", "type": "str"}], "response": {"module": "items", "schema_version": 2}},
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


def names(blockers: List[str]) -> List[str]:
    return sorted({b.split(":", 1)[0] for b in blockers})


class AdoptedCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-adopted22-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def copy(self, source: Path = WORKSHOP, name: str = "shop") -> Path:
        target = self.tmp / name
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv", ".fastapi-architect"))
        return target

    def write_json(self, name: str, doc: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def adoption_doc(self, project: Path, mount: dict = WORKSHOP_MOUNT, **over) -> dict:
        doc = {"schema": A11, "project": {"path": str(project)}, "modules": [ITEMS_MODULE], "package": "app", "unmanaged": [USERS, "app/storage.py"], "mount": dict(mount), "new_modules": json.loads(json.dumps(CONVENTIONS)), "dependencies": {"allowed": list(ALLOWED)}, "operations": list(OPERATIONS)}
        doc.update(over)
        return doc

    def adopt(self, project: Path, mount: dict = WORKSHOP_MOUNT, **over) -> dict:
        contract, errors = adoption.validate_adoption(self.adoption_doc(project, mount, **over))
        self.assertEqual(errors, [])
        return adoption.adopt(contract)

    def modules_contract(self, project: Path, modules: list, name: str = "modules.json") -> Path:
        return self.write_json(name, {"schema": SCHEMA, "project": {"path": str(project)}, "add_modules": modules})

    def changes_contract(self, project: Path, changes: list, name: str = "changes.json") -> Path:
        return self.write_json(name, {"schema": SCHEMA, "project": {"path": str(project)}, "modify_modules": changes})

    def plan(self, contract: Path) -> evolution.EvolutionPlan:
        return evolution.plan_evolution(evolution.load_evolution(str(contract)))

    def trusted(self, project: Path, **extra) -> evolution.EvolveOptions:
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        return evolution.EvolveOptions(apply=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project), declared_by="tests: fixture copied by this suite"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=300), test_command=command, **extra)

    def evolve(self, contract: Path, project: Path, **extra) -> dict:
        return evolution.run_evolution(str(contract), self.trusted(project, **extra))

    def pytest(self, project: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(project), capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def probe(self, project: Path, script: str) -> str:
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(project), capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc.stdout


# --------------------------------------------------------------------------- #
# contracts: adoption 1.1 and evolution 1.6
# --------------------------------------------------------------------------- #


class ContractTests(AdoptedCase):
    def test_adoption_1_0_still_validates_and_1_1_keys_need_1_1(self) -> None:
        old = {"schema": A10, "project": {"path": "p"}, "modules": [ITEMS_MODULE]}
        contract, errors = adoption.validate_adoption(old)
        self.assertEqual(errors, [])
        self.assertEqual(contract.schema, A10)
        self.assertEqual(contract.extensions, {})
        old["mount"] = dict(HANDMADE_MOUNT)
        _, errors = adoption.validate_adoption(old)
        self.assertTrue(any("mount" in e and "1.1" in e for e in errors), errors)
        contract, errors = adoption.validate_adoption(self.adoption_doc(Path("p")))
        self.assertEqual(errors, [])
        self.assertEqual(contract.schema, A11)
        self.assertEqual(sorted(contract.extensions), ["dependencies", "mount", "new_modules", "operations", "package", "unmanaged"])
        self.assertEqual(adoption.ADOPTION_SCHEMAS, (A10, A11))
        self.assertEqual(adopted.ADOPTION_OPERATIONS, ("add_module", "mount_router", "add_endpoint", "add_field", "add_persistence"))

    def test_malformed_1_1_extensions_are_rejected_with_their_paths(self) -> None:
        bad = self.adoption_doc(Path("p"), {"file": MAIN, "function": 3, "app": "app", "after_call": "register", "prefix": "api/"}, package="App", unmanaged="x", new_modules={"layout": "flat", "router_variable": "r", "import_style": "relative", "test_app": {"module": "app.main"}}, dependencies={"allowed": ["deps.require"]}, operations=["add_module", "drop_table"], alembic={"ini": 3, "script_location": "", "metadata": "x", "session_dependency": "y", "async": "yes"})
        contract, errors = adoption.validate_adoption(bad)
        self.assertIsNone(contract)
        joined = "\n".join(errors)
        for fragment in ("adoption.package", "adoption.unmanaged", "adoption.mount.function", "adoption.mount.after_call", "adoption.mount.prefix", "adoption.new_modules.layout", "adoption.new_modules.router_variable", "adoption.new_modules.import_style", "adoption.new_modules.test_app", "adoption.dependencies", "adoption.operations", "adoption.alembic"):
            self.assertIn(fragment, joined, joined)
        # new_modules without a mount, and a mount without new_modules, are each incomplete
        doc = self.adoption_doc(Path("p"))
        doc.pop("mount")
        _, errors = adoption.validate_adoption(doc)
        self.assertTrue(any("mount" in e for e in errors), errors)
        doc = self.adoption_doc(Path("p"))
        doc.pop("new_modules")
        _, errors = adoption.validate_adoption(doc)
        self.assertTrue(any("new_modules" in e for e in errors), errors)
        # under 1.1 the adopted modules may be absent when a mount is declared (a project adopted for new modules only)
        doc = self.adoption_doc(Path("p"), modules=[])
        contract, errors = adoption.validate_adoption(doc)
        self.assertEqual(errors, [])
        self.assertEqual(contract.modules, [])

    def test_evolution_1_6_extensions_need_1_6_and_are_validated(self) -> None:
        for key, value in (("persistence", "postgresql"), ("prefix", "/x"), ("dependencies", ["a.b:c"])):
            doc = {"schema": "fastapi-architect/evolution/1.5", "project": {"path": "p"}, "add_modules": [{"name": "m", "entities": [{"name": "M", "fields": [{"name": "x", "type": "str"}]}], key: value}]}
            _, errors = evolution.validate_evolution(doc)
            self.assertTrue(any(f"add_modules[0].{key}" in e and "needs schema" in e for e in errors), (key, errors))
        doc = {"schema": SCHEMA, "project": {"path": "p"}, "add_modules": [{"name": "m", "entities": [{"name": "M", "fields": [{"name": "x", "type": "str"}]}], "persistence": "sqlite", "prefix": "x/", "dependencies": ["nocolon"]}]}
        _, errors = evolution.validate_evolution(doc)
        joined = "\n".join(errors)
        for fragment in ("add_modules[0].persistence", "add_modules[0].prefix", "add_modules[0].dependencies"):
            self.assertIn(fragment, joined, joined)
        doc = {"schema": "fastapi-architect/evolution/1.5", "project": {"path": "p"}, "modify_modules": [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/x", "name": "x", "parameters": [], "response": {}}]}]}
        _, errors = evolution.validate_evolution(doc)
        self.assertTrue(any("add_endpoints[0].parameters: needs schema" in e for e in errors), errors)
        bad = {"schema": SCHEMA, "project": {"path": "p"}, "modify_modules": [{"name": "items", "add_endpoints": [
            {"method": "GET", "path": "/a/{id}", "name": "a", "parameters": [{"name": "other", "in": "path", "type": "str"}], "response": {}},
            {"method": "GET", "path": "/b", "name": "b", "parameters": [{"name": "q", "in": "query", "type": "int", "default": "nope"}], "delegate": {"callable": "app.services:count_items", "arguments": ["missing"]}, "response": {}},
            {"method": "GET", "path": "/c", "name": "c", "delegate": {"callable": "nocolon"}, "response_model": "lower", "dependencies": ["x"]},
            {"method": "GET", "path": "/d", "name": "d"},
        ]}]}
        _, errors = evolution.validate_evolution(bad)
        joined = "\n".join(errors)
        for fragment in ("{id} has no declared path parameter", "path parameter 'other' does not appear in the path", "parameters[0].default", "['missing'] are not parameters of the handler", "response and delegate are exclusive", "add_endpoints[2].delegate", "add_endpoints[2].response_model", "add_endpoints[2].dependencies", "add_endpoints[3].response: required"):
            self.assertIn(fragment, joined, joined)
        good = {"schema": SCHEMA, "project": {"path": "p"}, "modify_modules": [{"name": "items", "add_endpoints": EXTENDED}]}
        contract, errors = evolution.validate_evolution(good)
        self.assertEqual(errors, [])
        self.assertEqual(len(contract.modify_modules[0].add_endpoints), 3)
        self.assertEqual(evolution.REPORT_SCHEMA, "evolution_report/1.7")

    def test_the_proposal_lists_mount_candidates_and_a_skeleton_that_grants_nothing(self) -> None:
        proposal = adoption.propose_adoption(discover(WORKSHOP))
        self.assertEqual([(m["file"], m["function"], m["app"], m["registrations"], m["app_factories"]) for m in proposal["mount_candidates"]], [(MAIN, "create_app", "application", ["items_router", "users_router"], ["create_app"])])
        skeleton = proposal["adoption_1_1_skeleton"]
        self.assertEqual(skeleton["schema"], A11)
        self.assertEqual(skeleton["mount"], {"file": MAIN, "function": "create_app", "app": "application", "after_call": "application.include_router", "prefix": ""})
        self.assertEqual((skeleton["operations"], skeleton["dependencies"], skeleton["unmanaged"]), ([], {"allowed": []}, []))
        self.assertNotIn("alembic", skeleton)
        self.assertIsNone(proposal["alembic"])
        pg = adoption.propose_adoption(discover(NOTES_PG))
        self.assertEqual(pg["alembic"]["heads"], ["0001_notes"])
        self.assertEqual(pg["adoption_1_1_skeleton"]["alembic"], {"ini": "alembic.ini", "script_location": "alembic", "metadata": None, "session_dependency": None, "async": True})
        self.assertEqual(pg["mount_candidates"][0]["function"], None)
        text = adoption.render_proposal(proposal)
        self.assertIn("mount candidate: app/main.py::create_app", text)
        self.assertIn("nothing in it is a permission yet", text)


# --------------------------------------------------------------------------- #
# adopt: the boundary is verified and recorded; nothing else is written
# --------------------------------------------------------------------------- #


class AdoptTests(AdoptedCase):
    def test_adopt_1_1_verifies_the_mount_point_and_writes_only_the_record(self) -> None:
        shop = self.copy()
        before = tree(shop)
        report = self.adopt(shop)
        self.assertEqual(report["written"], [adoption.ADOPTION])
        self.assertEqual({k: v for k, v in tree(shop).items() if k != adoption.ADOPTION}, before)
        record = json.loads((shop / adoption.ADOPTION).read_text())
        self.assertEqual(record["schema"], A11)
        mount = record["mount"]
        self.assertEqual((mount["scope"], mount["existing_registrations"], mount["app_factories"], mount["file_sha256"]), ("create_app", ["items_router", "users_router"], ["create_app"], before[MAIN]))
        self.assertIn("after the last application.include_router(...) in create_app", mount["anchor"])
        self.assertEqual(record["operations"], OPERATIONS)
        self.assertEqual(record["dependencies"]["allowed"], ALLOWED)
        self.assertEqual(record["generated_modules"], {})
        self.assertIn("mount", record["boundary"])
        model = discover(shop)
        self.assertEqual(model.ownership_of(MAIN), "mount_point_unchanged")
        self.assertEqual(model.ownership_of(USERS), "unadopted")
        self.assertEqual(model.ownership_of(ITEMS), "adopted_unchanged")
        self.assertEqual(model.to_dict()["adoption"]["mount"]["function"], "create_app")
        # module-level application of the handmade shop
        hand = self.copy(HANDMADE, "hand")
        self.adopt(hand, HANDMADE_MOUNT, dependencies={"allowed": []})
        record = json.loads((hand / adoption.ADOPTION).read_text())
        self.assertEqual((record["mount"]["scope"], record["mount"]["function"]), ("module", None))

    def test_ambiguous_or_undeclared_boundaries_are_refused_and_nothing_is_written(self) -> None:
        shop = self.copy()
        main = (shop / MAIN).read_text()

        def refused(label: str, mutate=None, mount: dict = WORKSHOP_MOUNT, **over) -> List[str]:
            (shop / MAIN).write_text(main)
            if mutate:
                mutate()
            before = tree(shop)
            contract, errors = adoption.validate_adoption(self.adoption_doc(shop, mount, **over))
            self.assertEqual(errors, [], label)
            with self.assertRaises(adoption.AdoptionError, msg=label) as ctx:
                adoption.adopt(contract)
            self.assertEqual(tree(shop), before, label)
            self.assertFalse((shop / adoption.ADOPTION).exists(), label)
            return ctx.exception.errors

        self.assertTrue(any("application" in e or "app" in e for e in refused("wrong app name", mount={**WORKSHOP_MOUNT, "app": "app"})))
        self.assertTrue(any("create_app" in e or "function" in e for e in refused("function that does not exist", mount={**WORKSHOP_MOUNT, "function": "build"})))
        two = main + "\n\ndef create_other() -> FastAPI:\n    other = FastAPI()\n    other.include_router(items_router, prefix='/other')\n    return other\n"
        errors = refused("two factories, none named", lambda: (shop / MAIN).write_text(two), mount={**WORKSHOP_MOUNT, "function": None, "app": "app"})
        self.assertTrue(errors, errors)
        dynamic = main.replace('    application.include_router(users_router, prefix="/api/users")\n', '    for r in (users_router,):\n        application.include_router(r, prefix="/api/users")\n')
        self.assertTrue(any("dynamic" in e or "loop" in e or "compound" in e for e in refused("dynamic registration", lambda: (shop / MAIN).write_text(dynamic))))
        self.assertTrue(any(ITEMS in e for e in refused("mount file is an adopted file", mount={**WORKSHOP_MOUNT, "file": ITEMS, "function": None, "app": "router", "after_call": "router.include_router"})))
        self.assertTrue(any("app.deps:missing" in e for e in refused("dependency that does not resolve", dependencies={"allowed": ["app.deps:missing"]})))
        self.assertTrue(any("test_app" in e or "app.nowhere" in e for e in refused("test app that does not resolve", new_modules={**CONVENTIONS, "test_app": {"module": "app.nowhere", "attribute": "app"}})))
        self.assertTrue(any("package" in e for e in refused("package mismatch", package="shop")))
        self.assertTrue(any("alembic" in e for e in refused("alembic declared on a project without it", operations=OPERATIONS + ["add_persistence"], alembic={"ini": "alembic.ini", "script_location": "alembic", "metadata": "app.db:Base", "session_dependency": "app.db:get_session", "async": True})))
        real = shop / "app" / "main_real.py"
        shutil.move(str(shop / MAIN), str(real))
        os.symlink(real, shop / MAIN)
        try:
            contract, _ = adoption.validate_adoption(self.adoption_doc(shop))
            with self.assertRaises(adoption.AdoptionError) as ctx:
                adoption.adopt(contract)
            self.assertTrue(any("symlink" in e for e in ctx.exception.errors), ctx.exception.errors)
        finally:
            (shop / MAIN).unlink()
            shutil.move(str(real), str(shop / MAIN))


# --------------------------------------------------------------------------- #
# scenario A: a hand-written project without a database
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAS_FASTAPI, "fastapi, pytest and httpx are needed to run the fixture's own tests")
class ScenarioATests(AdoptedCase):
    def test_a_new_module_is_mounted_in_the_factory_and_verified_with_the_projects_suite(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        before = tree(shop)
        contract = self.modules_contract(shop, [REVIEWS])
        plan = self.plan(contract)
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.kind, "adopted")
        self.assertEqual(sorted(plan.create), ["app/reviews/__init__.py", "app/reviews/repository.py", "app/reviews/router.py", "app/reviews/schemas.py", "tests/test_reviews.py"])
        self.assertEqual(plan.modify, [MAIN, adoption.ADOPTION])
        self.assertEqual(plan.expected[MAIN], before[MAIN])
        added = [line for line in plan.diffs[MAIN].splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertEqual(added, ["+from app.reviews.router import router as reviews_router", '+    application.include_router(reviews_router, prefix="/api/reviews")'])
        self.assertFalse([line for line in plan.diffs[MAIN].splitlines() if line.startswith("-") and not line.startswith("---")], "nothing is removed from the mount file")
        self.assertEqual(plan.mount["steps"], [{"module": "reviews", "import": "from app.reviews.router import router as reviews_router", "statement": 'application.include_router(reviews_router, prefix="/api/reviews")'}])
        self.assertEqual([(r["method"], r["effective_path"]) for r in plan.mount["expected_routes"]], [("POST", "/api/reviews"), ("GET", "/api/reviews/{review_id}"), ("GET", "/api/reviews")])
        self.assertEqual(plan.generated_modules["reviews"]["persistence"], "in_memory")
        self.assertFalse(plan.migration["required"])
        self.assertEqual([c["kind"] for c in plan.changes], ["add_module"])
        self.assertEqual(plan.public_contracts["routes_added"], [{"method": "POST", "path": "/api/reviews", "module": "reviews"}, {"method": "GET", "path": "/api/reviews/{review_id}", "module": "reviews"}, {"method": "GET", "path": "/api/reviews", "module": "reviews"}])
        self.assertTrue(any(t["path"] == MAIN and t["sha256_before"] == before[MAIN] for t in plan.transforms))
        self.assertEqual(tree(shop), before, "plan is a dry run")
        report = self.evolve(contract, shop)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        self.assertEqual(report["schema"], "evolution_report/1.7")
        self.assertEqual(report["verification"]["mount"], {"file": MAIN, "expected_routes": 3, "unresolved": []})
        self.assertEqual(report["verification"]["tests"]["run"]["outcome"], "passed")
        self.assertEqual(report["plan"]["mount"]["file"], MAIN)
        self.assertIn("reviews", report["plan"]["generated_modules"])
        after = tree(shop)
        self.assertEqual(after[USERS], before[USERS])
        self.assertEqual(after["app/storage.py"], before["app/storage.py"])
        self.assertEqual(after[ITEMS], before[ITEMS])
        main = (shop / MAIN).read_text()
        original = (WORKSHOP / MAIN).read_text()
        self.assertEqual(main.replace("from app.reviews.router import router as reviews_router\n", "").replace('    application.include_router(reviews_router, prefix="/api/reviews")\n', ""), original)
        record = json.loads((shop / adoption.ADOPTION).read_text())
        self.assertEqual(sorted(record["generated_modules"]["reviews"]["files"]), sorted(plan.create))
        self.assertEqual(record["mount"]["file_sha256"], after[MAIN])
        self.assertEqual(record["history"][-1]["added_modules"], ["reviews"])
        model = discover(shop)
        self.assertEqual(model.ownership_of("app/reviews/router.py"), "generated_unchanged")
        self.assertEqual(model.ownership_of(MAIN), "mount_point_unchanged")
        proc = self.pytest(shop)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertIn("6 passed", proc.stdout)
        out = self.probe(shop, "from fastapi.testclient import TestClient\nfrom app.main import app\nc = TestClient(app)\nr = c.post('/api/reviews', json={'author': 'ann', 'rating': 5})\nprint(r.status_code, c.get('/api/reviews/' + r.json()['id']).json()['author'], c.get('/api/items').status_code, c.get('/health').json())\n")
        self.assertEqual(out.strip(), "201 ann 200 {'ok': True}")
        # D: the same contract again is a no-op; a second module chains after the first registration
        self.assertEqual(self.plan(self.modules_contract(shop, [REVIEWS], name="again.json")).state, "noop")
        plan = self.plan(self.modules_contract(shop, [REVIEWS, SUPPLIERS], name="second.json"))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual([c["detail"] for c in plan.changes if c["target"] == "add_modules.reviews"], ["already generated with this definition; no-op"])
        added = [line for line in plan.diffs[MAIN].splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertEqual(added, ["+from app.suppliers.router import router as suppliers_router", '+    application.include_router(suppliers_router, prefix="/api/suppliers")'])
        self.assertIn('application.include_router(reviews_router, prefix="/api/reviews")\n    application.include_router(suppliers_router', plan.files[MAIN])

    def test_a_extended_endpoints_delegate_depend_and_type_their_parameters(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        before = tree(shop)
        contract = self.changes_contract(shop, [{"name": "items", "add_endpoints": EXTENDED}])
        plan = self.plan(contract)
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.modify, [ITEMS, adoption.ADOPTION])
        diff = plan.diffs[ITEMS]
        removed = [line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(removed, [])
        for fragment in ("from app.services import count_items", "from app.schemas import ItemStats", "from typing import Annotated", "from fastapi import Query", "from app.deps import require_api_key", "from fastapi import Depends", "async def items_stats(min_price: Annotated[float, Query()] = 0.0) -> ItemStats:", "    return count_items(min_price)", "async def items_value(_require_api_key: Annotated[object, Depends(require_api_key)]) -> dict:", "    return await catalogue_value()", "async def items_ping(tag: str) -> dict:"):
            self.assertIn(fragment, plan.files[ITEMS], fragment)
        self.assertIn("# explicit 404", plan.files[ITEMS])
        self.assertIn("# sync on purpose", plan.files[ITEMS])
        report = self.evolve(contract, shop)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        self.assertEqual(tree(shop)[USERS], before[USERS])
        out = self.probe(shop, "from fastapi.testclient import TestClient\nfrom app.main import app\nc = TestClient(app)\nc.post('/api/items', json={'name': 'lamp', 'price': 12.5}); c.post('/api/items', json={'name': 'pin', 'price': 1})\nprint(c.get('/api/items/reports/stats').json(), c.get('/api/items/reports/stats?min_price=10').json()['count'], c.get('/api/items/reports/value').status_code, c.get('/api/items/reports/value', headers={'X-API-Key': 'workshop-key'}).json(), c.get('/api/items/ping/x').json()['schema_version'], c.get('/api/items/1').json()['name'])\n")
        self.assertEqual(out.strip(), "{'count': 2, 'min_price': 0.0} 1 401 {'total': 13.5} 2 lamp")
        record = json.loads((shop / adoption.ADOPTION).read_text())
        self.assertEqual([r["function"] for r in record["modules"]["items"]["routes"]][-3:], ["items_stats", "items_value", "items_ping"])
        again = self.plan(self.changes_contract(shop, [{"name": "items", "add_endpoints": EXTENDED[:1]}], name="again.json"))
        self.assertEqual(again.state, "blocked")
        self.assertTrue(any("duplicate" in b for b in again.blockers), again.blockers)

    def test_a_generated_modules_evolve_additively_and_manual_edits_to_them_block(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        report = self.evolve(self.modules_contract(shop, [SUPPLIERS]), shop)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        after_module = tree(shop)
        change = self.changes_contract(shop, [{"name": "suppliers", "entities": [{"name": "Supplier", "add_fields": [{"name": "active", "type": "bool", "required": True, "default": True}], "add_operations": ["list"]}]}])
        plan = self.plan(change)
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(plan.create, [])
        self.assertEqual(sorted(plan.modify), sorted([adoption.ADOPTION, "app/suppliers/repository.py", "app/suppliers/router.py", "app/suppliers/schemas.py", "tests/test_suppliers.py"]))
        self.assertNotIn(MAIN, plan.modify, "the mount file is not touched by a change inside a generated module")
        self.assertEqual(sorted((c["kind"], c["classification"]) for c in plan.changes), [("add_field", "compatible"), ("add_operation", "compatible")])
        self.assertIn("async def list_suppliers(", plan.files["app/suppliers/router.py"])
        self.assertIn("active: bool = Field(default=True)", plan.files["app/suppliers/schemas.py"])
        self.assertFalse(plan.migration["required"])
        report = self.evolve(change, shop)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        self.assertEqual(tree(shop)[MAIN], after_module[MAIN])
        self.assertEqual(self.plan(self.changes_contract(shop, [{"name": "suppliers", "entities": [{"name": "Supplier", "add_fields": [{"name": "active", "type": "bool", "required": True, "default": True}], "add_operations": ["list"]}]}], name="again.json")).state, "noop")
        out = self.probe(shop, "from fastapi.testclient import TestClient\nfrom app.main import app\nc = TestClient(app)\nr = c.post('/api/suppliers', json={'name': 'acme'})\nprint(r.json()['active'], len(c.get('/api/suppliers').json()))\n")
        self.assertEqual(out.strip(), "True 1")
        blocked = self.plan(self.changes_contract(shop, [{"name": "suppliers", "entities": [{"name": "Supplier", "add_fields": [{"name": "rating", "type": "int", "required": True}]}]}], name="required.json"))
        self.assertEqual(names(blocked.blockers), ["field_compatible"])
        router = shop / "app" / "suppliers" / "router.py"
        router.write_text(router.read_text() + "\n# a manual note inside a generated module\n")
        blocked = self.plan(self.changes_contract(shop, [{"name": "suppliers", "entities": [{"name": "Supplier", "add_operations": ["delete"]}]}], name="drift.json"))
        self.assertEqual(names(blocked.blockers), ["generated_module_files_unchanged"])
        self.assertEqual(discover(shop).ownership_of("app/suppliers/router.py"), "generated_modified")
        blocked = self.plan(self.changes_contract(shop, [{"name": "suppliers", "add_endpoints": [{"method": "GET", "path": "/x", "name": "x", "response": {}}]}], name="style.json"))
        self.assertEqual(names(blocked.blockers), ["change_supported_for_generated"])

    def test_the_handmade_shop_mounts_at_module_level(self) -> None:
        shop = self.copy(HANDMADE, "hand")
        self.adopt(shop, HANDMADE_MOUNT, unmanaged=[USERS, "app/storage.py"], dependencies={"allowed": []})
        report = self.evolve(self.modules_contract(shop, [REVIEWS]), shop)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        main = (shop / MAIN).read_text()
        self.assertIn('app.include_router(users_router, prefix="/api/users")\napp.include_router(reviews_router, prefix="/api/reviews")\n', main)
        self.assertEqual(main.replace("from app.reviews.router import router as reviews_router\n", "").replace('app.include_router(reviews_router, prefix="/api/reviews")\n', ""), (HANDMADE / MAIN).read_text())
        self.assertEqual(sha(shop / USERS), sha(HANDMADE / USERS))


# --------------------------------------------------------------------------- #
# scenario C and the journal: concurrency, interruption, recovery, retry
# --------------------------------------------------------------------------- #


class ConcurrencyTests(AdoptedCase):
    def test_c_the_mount_file_changed_after_the_plan_is_refused_and_nothing_is_written(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        plan = self.plan(self.modules_contract(shop, [REVIEWS]))
        self.assertEqual(plan.state, "planned", plan.blockers)
        main = shop / MAIN
        main.write_text(main.read_text() + "\n# edited while the operator read the plan\n")
        snapshot = tree(shop)
        with self.assertRaises(journal.WriteError) as ctx:
            evolution._write_plan(shop, plan)
        self.assertIn("concurrent change", str(ctx.exception))
        self.assertIn(MAIN, str(ctx.exception))
        self.assertEqual(tree(shop), snapshot)
        self.assertEqual(journal.pending_operations(shop), [])
        self.assertFalse((shop / "app" / "reviews").exists())
        again = self.plan(self.modules_contract(shop, [REVIEWS], name="again.json"))
        self.assertEqual(names(again.blockers), ["mount_file_unchanged_since_adoption"])
        self.adopt(shop)  # the operator re-verifies the mount point
        self.assertEqual(self.plan(self.modules_contract(shop, [REVIEWS], name="third.json")).state, "planned")
        # a plan whose mount edit was tampered with after verification is refused by the writer too
        plan = self.plan(self.modules_contract(shop, [REVIEWS], name="tamper.json"))
        plan.files[MAIN] = plan.files[MAIN] + "# tampered\n"
        with self.assertRaises(journal.WriteError) as ctx:
            evolution._write_plan(shop, plan)
        self.assertIn("mount edit", str(ctx.exception))
        self.assertEqual(journal.pending_operations(shop), [])

    def test_lock_interruption_recovery_retry_and_the_journal_steps(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        contract = self.modules_contract(shop, [REVIEWS])
        lock = acquire(shop, kind="test", description="another process")
        try:
            report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
            self.assertEqual(report["applied"]["state"], "locked")
        finally:
            lock.release()
        before = tree(shop)
        faults = journal.FaultInjector(points=["after_publish:2"])
        report = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False, faults=faults))
        self.assertEqual(report["applied"]["state"], "interrupted", report["applied"])
        pending = journal.pending_operations(shop)
        self.assertEqual(len(pending), 1)
        kinds = [s["kind"] for s in pending[0].steps]
        self.assertEqual(kinds, ["generate", "mount", "manifest"])
        self.assertEqual(pending[0].steps[1]["evidence"][MAIN]["steps"][0]["module"], "reviews")
        self.assertEqual(names(self.plan(self.modules_contract(shop, [REVIEWS], name="pending.json")).blockers), ["no_pending_operation"])
        outcome = journal.recover(shop, pending[0], strategy="rollback")
        self.assertEqual(outcome.state, "rolled_back")
        self.assertEqual(tree(shop), before, "the rollback restores the mount file and removes the published module files")
        retry = evolution.run_evolution(str(contract), evolution.EvolveOptions(apply=True, verify=False))
        self.assertEqual(retry["applied"]["state"], "applied", retry["plan"]["blockers"])
        self.assertTrue((shop / "app" / "reviews" / "router.py").is_file())
        self.assertEqual(self.plan(self.modules_contract(shop, [REVIEWS], name="noop.json")).state, "noop")
        # a change to an adopted router records a transform step, not a mount step
        faults = journal.FaultInjector(points=["after_publish:1"])
        report = evolution.run_evolution(str(self.changes_contract(shop, [{"name": "items", "add_endpoints": EXTENDED[2:]}])), evolution.EvolveOptions(apply=True, verify=False, faults=faults))
        pending = journal.pending_operations(shop)
        self.assertEqual([s["kind"] for s in pending[0].steps], ["transform", "manifest"])
        self.assertEqual(journal.recover(shop, pending[0], strategy="rollback").state, "rolled_back")


# --------------------------------------------------------------------------- #
# adversarial controls: every refusal leaves the project intact
# --------------------------------------------------------------------------- #


class AdversarialTests(AdoptedCase):
    def blocked(self, shop: Path, contract: Path, expected: str) -> evolution.EvolutionPlan:
        before = tree(shop)
        plan = self.plan(contract)
        self.assertEqual(plan.state, "blocked", (expected, plan.state, plan.blockers))
        self.assertIn(expected, names(plan.blockers), plan.blockers)
        self.assertEqual(tree(shop), before, expected)
        return plan

    def test_module_generation_refusals(self) -> None:
        shop = self.copy()
        self.adopt(shop)
        entity = [{"name": "Thing", "fields": [{"name": "x", "type": "str"}]}]
        self.blocked(shop, self.modules_contract(shop, [{"name": "items", "entities": entity}], "adopted.json"), "module_is_new")
        self.blocked(shop, self.modules_contract(shop, [{"name": "storage", "entities": entity}], "sibling.json"), "file_in_the_way")
        self.blocked(shop, self.modules_contract(shop, [{"name": "routers", "entities": entity}], "package.json"), "file_in_the_way")
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity, "prefix": "/api/users"}], "prefix.json"), "mount_prefix_conflict")
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity, "dependencies": ["app.deps:other"]}], "dependency.json"), "dependency_allowed")
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity, "persistence": "postgresql"}], "persistence.json"), "persistence_unsupported_for_adopted")
        self.blocked(shop, self.modules_contract(shop, [{"name": "a", "entities": entity, "prefix": "/same"}, {"name": "b", "entities": entity, "prefix": "/same"}], "twice.json"), "mount_prefix_conflict")
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": [{"name": "Thing", "fields": [{"name": "x", "type": "str"}, {"name": "x", "type": "int"}]}]}], "fragment.json"), "module_definition_valid")
        # an unmanaged glob covering a generated path
        self.adopt(shop, unmanaged=[USERS, "tests/*"])
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "unmanaged.json"), "unmanaged_path")
        # operations without add_module; a 1.0 adoption without a mount
        self.adopt(shop, operations=["add_endpoint"])
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "ops.json"), "adoption_operation_allowed")
        contract, errors = adoption.validate_adoption({"schema": A10, "project": {"path": str(shop)}, "modules": [ITEMS_MODULE]})
        self.assertEqual(errors, [])
        adoption.adopt(contract)
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "legacy.json"), "change_supported_for_adopted")
        # the router alias is already bound in the mount file
        main = shop / MAIN
        main.write_text(main.read_text().replace("app = create_app()\n", "accounts_router = None\napp = create_app()\n"))
        self.adopt(shop)
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "alias.json"), "mount_symbol_free")
        # the mount file became a symlink, with the same content
        main.write_text((WORKSHOP / MAIN).read_text())
        self.adopt(shop)
        real = shop / "app" / "main_real.py"
        shutil.move(str(main), str(real))
        os.symlink(real, main)
        try:
            plan = self.plan(self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "symlink.json"))
            self.assertEqual(plan.state, "blocked")
            self.assertIn("unsafe_path", names(plan.blockers), plan.blockers)
        finally:
            main.unlink()
            shutil.move(str(real), str(main))
        # a mount file that no longer parses: its digest changed, so the baseline is stale
        main.write_text(main.read_text() + "\ndef broken(:\n")
        self.blocked(shop, self.modules_contract(shop, [{"name": "accounts", "entities": entity}], "broken.json"), "mount_file_unchanged_since_adoption")

    def test_extension_refusals(self) -> None:
        shop = self.copy()
        self.adopt(shop)

        def endpoint(**over) -> dict:
            return {"method": "GET", "path": "/reports/extra", "name": "items_extra", "response": {"ok": True}, **over}

        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(dependencies=["app.deps:other"])]}], "dep.json"), "dependency_allowed")
        services = shop / "app" / "services.py"
        original_services = services.read_text()
        services.write_text(original_services.replace("async def catalogue_value() -> dict:", "async def catalogue_value_renamed() -> dict:"))  # a manual file changed after the adoption resolved it
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/reports/value", "name": "items_value", "delegate": {"callable": "app.services:catalogue_value", "arguments": []}}]}], "gone.json"), "delegate_resolves")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(dependencies=["app.services:catalogue_value"])]}], "dep2.json"), "dependency_resolves")
        services.write_text(original_services)
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(path="/stats")]}], "shadow.json"), "route_not_shadowed")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(path="/{code}", name="by_code", parameters=[{"name": "code", "in": "path", "type": "str"}])]}], "shadow2.json"), "route_not_shadowed")
        self.assertEqual(self.plan(self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(path="/stats", method="POST")]}], "other-method.json")).state, "planned", "another method is not captured by GET /{item_id}")
        self.adopt(shop, dependencies={"allowed": ALLOWED + ["app.schemas:ItemStats"]})
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/reports/x", "name": "items_x", "delegate": {"callable": "app.schemas:ItemStats", "arguments": []}}]}], "class.json"), "delegate_resolves")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [{"method": "GET", "path": "/reports/extra", "name": "items_extra", "delegate": {"callable": "app.services:nowhere", "arguments": []}}]}], "missing.json"), "delegate_resolves")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(response_model="UserOut")]}], "model.json"), "response_model_found")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(name="list_items", path="")]}], "dup.json"), "transform_preconditions")
        self.blocked(shop, self.changes_contract(shop, [{"name": "users", "add_endpoints": [endpoint()]}], "unadopted.json"), "module_is_adopted")
        self.adopt(shop, operations=["add_module", "mount_router"])
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint(parameters=[{"name": "q", "in": "query", "type": "int"}])]}], "ops.json"), "adoption_operation_allowed")
        items = shop / ITEMS
        items.write_text(items.read_text() + "\n# manual\n")
        self.blocked(shop, self.changes_contract(shop, [{"name": "items", "add_endpoints": [endpoint()]}], "drift.json"), "adopted_files_unchanged_since_adoption")
        self.assertEqual(sha(shop / USERS), sha(WORKSHOP / USERS))

    def test_the_verification_rejects_a_module_whose_routes_do_not_resolve(self) -> None:
        if not HAS_FASTAPI:
            self.skipTest("fastapi needed")
        shop = self.copy()
        self.adopt(shop)
        plan = self.plan(self.modules_contract(shop, [REVIEWS]))
        plan.mount["expected_routes"].append({"method": "GET", "path": "/missing", "effective_path": "/api/reviews/missing", "function": "nobody"})
        result = evolution.verify_in_copy(plan, self.trusted(shop))
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["mount"]["unresolved"], ["GET /api/reviews/missing (nobody)"])
        self.assertFalse((shop / "app" / "reviews").exists())


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #


class CommandLineTests(AdoptedCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=str(REPO_ROOT), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)

    def test_propose_adopt_and_plan_through_the_command_line(self) -> None:
        shop = self.copy()
        proc = self.run_cli("propose", str(shop), "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proposal = json.loads(proc.stdout)
        self.assertEqual(proposal["mount_candidates"][0]["function"], "create_app")
        contract = self.write_json("adopt.json", self.adoption_doc(shop))
        proc = self.run_cli("adopt", str(contract))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("adopted items", proc.stdout)
        bad = self.write_json("bad.json", self.adoption_doc(shop, {**WORKSHOP_MOUNT, "app": "app"}))
        proc = self.run_cli("adopt", str(bad))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("adoption refused", proc.stderr)
        before = tree(shop)
        proc = self.run_cli("plan", str(self.modules_contract(shop, [REVIEWS])), "--no-verify", "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual((report["state"], report["schema"]), ("planned", "evolution_report/1.7"))
        self.assertEqual(report["plan"]["mount"]["steps"][0]["module"], "reviews")
        self.assertEqual(report["plan"]["files"]["create"][:1], ["app/reviews/__init__.py"])
        self.assertEqual(tree(shop), before, "plan is a dry run")


if __name__ == "__main__":
    unittest.main()
