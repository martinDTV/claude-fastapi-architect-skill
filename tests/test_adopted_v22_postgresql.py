"""Increment 4 of v2.2, scenario B on a real PostgreSQL: a hand-written project with SQLAlchemy (async) and Alembic
(``tests/fixtures/adoption22/notes_pg``) is adopted with its own Alembic environment declared and verified, a module
with PostgreSQL persistence is generated after the current head, the project's own ``alembic upgrade head`` creates
the table, the module round-trips against the database, the downgrade is reversible, a later change adds a column
through a second additive revision, and every ambiguous Alembic situation (two heads, a table that exists, an env.py
that targets another metadata, a session dependency that does not resolve, a script location that is not the ini's)
is refused with the project intact.

Opt-in like the other PostgreSQL modules: FA_DESIGN_PG=1, PostgreSQL binaries and uv on the PATH. A throwaway
cluster is created per class; nothing of the user's is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict

from _audit_support import FIXTURES, SKILL_SCRIPTS
from _pg_support import ThrowawayCluster, pg_available

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_common.artefacts import walk_files  # noqa: E402
from fastapi_design import adoption, evolution  # noqa: E402
from fastapi_design.discovery import discover  # noqa: E402

ENABLED = os.environ.get("FA_DESIGN_PG") == "1"
EVIDENCE_DIR = os.environ.get("FA_DESIGN_PG_EVIDENCE")
NOTES_PG = FIXTURES / "adoption22" / "notes_pg"
SUITE = "uv run --group dev pytest -q -p no:cacheprovider"
MAIN = "app/main.py"
ALEMBIC = {"ini": "alembic.ini", "script_location": "alembic", "metadata": "app.db:Base", "session_dependency": "app.db:get_session", "async": True}
TASKS = {"name": "tasks", "description": "Tasks", "persistence": "postgresql", "entities": [{"name": "Task", "fields": [{"name": "title", "type": "str", "max_length": 120}, {"name": "done", "type": "bool", "required": True, "default": False}, {"name": "due", "type": "date", "required": False}]}]}
DRIVER = (
    "from fastapi.testclient import TestClient\nfrom app.main import app\n"
    "c = TestClient(app).__enter__()\n"
    "r = c.post('/api/tasks', json={'title': 'write docs', 'due': '2026-11-02'})\nb = r.json()\n"
    "print(r.status_code, b['done'], b['due'], c.get('/api/tasks/' + b['id']).json() == b, len(c.get('/api/tasks').json()), c.patch('/api/tasks/' + b['id'], json={'done': True}).json()['done'], c.delete('/api/tasks/' + b['id']).status_code, c.get('/api/notes').status_code, c.get('/health').json())\n"
)


def _uv_env() -> Dict[str, str]:
    home = Path.home()
    return {"UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(home / ".cache" / "uv")), "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR", str(home / ".local" / "share" / "uv" / "python"))}


def tree(root: Path) -> Dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in walk_files(root) if ".fastapi-architect/ops" not in p.as_posix()}


@unittest.skipUnless(ENABLED, "set FA_DESIGN_PG=1 to run the adopted-project persistence scenario against a throwaway cluster")
@unittest.skipUnless(pg_available() and shutil.which("uv"), "PostgreSQL binaries and uv are required")
class AdoptedPersistencePostgresTests(unittest.TestCase):
    TIMINGS = "adopted-timings.json"

    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = ThrowawayCluster().__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="fa-adopted-pg-"))
        cls.timings: Dict[str, object] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / cls.TIMINGS).write_text(json.dumps(cls.timings, indent=2, sort_keys=True), encoding="utf-8")

    # -- helpers ------------------------------------------------------------- #

    def project_copy(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(NOTES_PG, target, ignore=shutil.ignore_patterns("__pycache__", ".venv", ".fastapi-architect", ".ruff_cache"))
        return target

    def tool(self, project: Path, url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ, **_uv_env(), NOTES_DATABASE_URL=url)
        proc = subprocess.run(["uv", "run", "--group", "dev", *args], cwd=str(project), env=env, capture_output=True, text=True, timeout=600)
        if check:
            self.assertEqual(proc.returncode, 0, f"{' '.join(args)}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        return proc

    def sql(self, db: str, statement: str) -> str:
        return self.cluster.psql(statement, db=db)

    def timed(self, label: str, started: float) -> None:
        self.timings[label] = round(time.time() - started, 2)

    def write(self, name: str, doc: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def adoption_doc(self, project: Path, **over) -> dict:
        doc = {"schema": adoption.ADOPTION_SCHEMA_11, "project": {"path": str(project)}, "modules": [{"name": "notes", "router": {"file": "app/routers/notes.py", "name": "router"}, "files": ["app/routers/notes.py", "app/schemas.py"]}], "package": "app", "unmanaged": ["alembic/versions/0001_notes.py", "app/models.py"], "mount": {"file": MAIN, "function": None, "app": "app", "after_call": "app.include_router", "prefix": "/api"}, "new_modules": {"layout": "package", "router_variable": "router", "import_style": "absolute", "test_app": {"module": "app.main", "attribute": "app"}}, "operations": ["add_module", "mount_router", "add_persistence"], "alembic": dict(ALEMBIC)}
        doc.update(over)
        return doc

    def adopt(self, project: Path, **over) -> dict:
        contract, errors = adoption.validate_adoption(self.adoption_doc(project, **over))
        self.assertEqual(errors, [])
        return adoption.adopt(contract)

    def evolve(self, project: Path, contract: Path, url: str) -> dict:
        for key, value in _uv_env().items():
            os.environ[key] = value
        os.environ["NOTES_DATABASE_URL"] = url
        options = evolution.EvolveOptions(apply=True, verify=True, authorization=Authorization(execution=True, trust="fixture", scope=str(project.parent), declared_by="tests: v2.2 adopted persistence, FA_DESIGN_PG opt-in"), policy=ExecutionPolicy(authorized=True, command=SUITE, timeout_s=900, env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + ("NOTES_DATABASE_URL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")), test_command=SUITE)
        return evolution.run_evolution(str(contract), options)

    def plan(self, project: Path, name: str, **body) -> evolution.EvolutionPlan:
        return evolution.plan_evolution(evolution.load_evolution(str(self.write(name, {"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(project)}, **body}))))

    # -- scenario B ---------------------------------------------------------- #

    def test_01_a_postgresql_module_is_generated_after_the_head_migrated_round_tripped_and_reversible(self) -> None:
        url = self.cluster.create_database("notes_b")
        project = self.project_copy("notes")
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("baseline_upgrade_s", started)
        self.assertEqual(self.sql("notes_b", "SELECT version_num FROM alembic_version"), "0001_notes")
        before = tree(project)
        report = self.adopt(project)
        self.assertEqual(report["written"], [adoption.ADOPTION])
        self.assertEqual({k: v for k, v in tree(project).items() if k != adoption.ADOPTION}, before)
        record = json.loads((project / adoption.ADOPTION).read_text())
        verified = record["alembic"]["verified"]
        self.assertEqual((verified["head"], verified["env_target_metadata"], verified["metadata"]["kind"], verified["session_dependency"]["is_async"], sorted(verified["tables"])), ("0001_notes", {"imported_from": "app.db:Base", "name": "Base"}, "class", True, ["notes"]))
        contract = self.write("tasks.json", {"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(project)}, "add_modules": [TASKS]})
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract)))
        self.assertEqual(plan.state, "planned", plan.blockers)
        revision = plan.generated_modules["tasks"]["revision"]
        self.assertTrue(revision)
        rev_file = f"alembic/versions/{revision}_tasks.py"
        self.assertEqual(sorted(plan.create), sorted([rev_file, "app/tasks/__init__.py", "app/tasks/model.py", "app/tasks/repository.py", "app/tasks/router.py", "app/tasks/schemas.py", "tests/test_tasks.py"]))
        self.assertEqual(plan.modify, [MAIN, adoption.ADOPTION])
        self.assertEqual((plan.migration["required"], plan.migration["head_before"], plan.migration["head_after"], plan.migration["tables"], plan.migration["files"], plan.migration["reversible"]), (True, "0001_notes", revision, ["tasks"], [rev_file], True))
        self.assertIn('down_revision = "0001_notes"', plan.files[rev_file])
        self.assertIn('op.create_table(\n        "tasks",', plan.files[rev_file])
        self.assertIn('op.drop_table("tasks")', plan.files[rev_file])
        self.assertIn("from app.db import Base", plan.files["app/tasks/model.py"])
        self.assertIn('__tablename__ = "tasks"', plan.files["app/tasks/model.py"])
        self.assertIn("from app.db import get_session", plan.files["app/tasks/router.py"])
        self.assertIn("session: Annotated[AsyncSession, Depends(get_session)]", plan.files["app/tasks/router.py"])
        self.assertEqual(tree(project), {**before, adoption.ADOPTION: tree(project)[adoption.ADOPTION]}, "plan is a dry run")
        started = time.time()
        report = self.evolve(project, contract, url)
        self.timed("evolve_tasks_verified_s", started)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        self.assertEqual(report["verification"]["mount"]["unresolved"], [])
        self.assertEqual(report["verification"]["tests"]["run"]["outcome"], "passed")
        self.assertEqual(tree(project)["alembic/versions/0001_notes.py"], before["alembic/versions/0001_notes.py"], "the historical revision is untouched")
        self.assertEqual(tree(project)["app/models.py"], before["app/models.py"])
        started = time.time()
        self.tool(project, url, "alembic", "upgrade", "head")
        self.timed("upgrade_tasks_s", started)
        self.assertEqual(self.sql("notes_b", "SELECT version_num FROM alembic_version"), revision)
        columns = self.sql("notes_b", "SELECT column_name || ':' || is_nullable || ':' || coalesce(column_default, '-') FROM information_schema.columns WHERE table_name = 'tasks' ORDER BY ordinal_position").splitlines()
        self.assertEqual(columns, ["id:NO:-", "title:NO:-", "done:NO:false", "due:YES:-", "created_at:NO:now()"])
        out = self.tool(project, url, "python", "-c", DRIVER).stdout.strip()
        self.assertEqual(out, "201 False 2026-11-02 True 1 True 204 200 {'ok': True}")
        self.tool(project, url, "alembic", "downgrade", "-1")
        self.assertEqual(self.sql("notes_b", "SELECT count(*) FROM information_schema.tables WHERE table_name = 'tasks'"), "0")
        self.assertEqual(self.sql("notes_b", "SELECT version_num FROM alembic_version"), "0001_notes")
        self.tool(project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql("notes_b", "SELECT version_num FROM alembic_version"), revision)
        record = json.loads((project / adoption.ADOPTION).read_text())
        self.assertEqual(record["alembic"]["verified"]["head"], revision)
        self.assertEqual([m["revision"] for m in record["migrations"]], [revision])
        self.assertEqual(record["migrations"][0]["down_revision"], "0001_notes")
        self.assertEqual(discover(project).alembic["heads"], [revision])
        # a second contract: the generated module gains a defaulted column through a second additive revision
        contract2 = self.write("priority.json", {"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(project)}, "modify_modules": [{"name": "tasks", "entities": [{"name": "Task", "add_fields": [{"name": "priority", "type": "int", "required": True, "default": 1}]}]}]})
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract2)))
        self.assertEqual(plan.state, "planned", plan.blockers)
        self.assertEqual(len(plan.create), 1)
        rev2_file = plan.create[0]
        rev2 = plan.generated_modules["tasks"]["revision"]
        self.assertEqual((plan.migration["head_before"], plan.migration["head_after"], plan.migration["columns"], plan.migration["tables"]), (revision, rev2, ["tasks.priority"], []))
        self.assertIn(f'down_revision = "{revision}"', plan.files[rev2_file])
        self.assertIn('op.add_column("tasks", sa.Column("priority", sa.Integer(), server_default=sa.text("1"), nullable=False))', plan.files[rev2_file])
        self.assertIn('op.drop_column("tasks", "priority")', plan.files[rev2_file])
        self.assertEqual(sorted(plan.modify), sorted([adoption.ADOPTION, "app/tasks/model.py", "app/tasks/schemas.py"]))
        report = self.evolve(project, contract2, url)
        self.assertEqual(report["state"], "verified", report.get("verification", {}).get("reasons"))
        self.tool(project, url, "alembic", "upgrade", "head")
        self.assertEqual(self.sql("notes_b", "SELECT version_num FROM alembic_version"), rev2)
        self.assertEqual(self.sql("notes_b", "SELECT is_nullable || ':' || column_default FROM information_schema.columns WHERE table_name = 'tasks' AND column_name = 'priority'"), "NO:1")
        out = self.tool(project, url, "python", "-c", DRIVER.replace("b['done'], b['due']", "b['done'], b['priority']")).stdout.strip()
        self.assertEqual(out, "201 False 1 True 1 True 204 200 {'ok': True}")
        self.assertEqual(self.plan(project, "again.json", modify_modules=[{"name": "tasks", "entities": [{"name": "Task", "add_fields": [{"name": "priority", "type": "int", "required": True, "default": 1}]}]}]).state, "noop")
        stale = self.plan(project, "stale.json", add_modules=[TASKS])
        self.assertEqual(stale.state, "blocked")
        self.assertTrue(any(b.startswith("module_is_new") for b in stale.blockers), "the original definition is no longer the module's: it gained a field")
        current = json.loads(json.dumps(TASKS))
        current["entities"][0]["fields"].append({"name": "priority", "type": "int", "required": True, "default": 1})
        self.assertEqual(self.plan(project, "again2.json", add_modules=[current]).state, "noop")

    def test_02_ambiguous_alembic_situations_are_refused_with_the_project_intact(self) -> None:
        project = self.project_copy("notes_adv")
        self.adopt(project)
        snapshot = tree(project)

        def blocked(name: str, expected: str, **body) -> None:
            plan = self.plan(project, name, **body)
            self.assertEqual(plan.state, "blocked", (expected, plan.state, plan.blockers))
            self.assertTrue(any(b.startswith(expected) for b in plan.blockers), (expected, plan.blockers))
            self.assertEqual(tree(project), snapshot, expected)

        entity = [{"name": "Thing", "fields": [{"name": "x", "type": "str"}]}]
        blocked("dup.json", "table_name_free", add_modules=[{"name": "journal", "persistence": "postgresql", "entities": [{"name": "Note", "fields": [{"name": "x", "type": "str"}]}]}])
        blocked("twice.json", "table_name_free", add_modules=[{"name": "a", "persistence": "postgresql", "entities": [{"name": "Thing", "fields": [{"name": "x", "type": "str"}]}]}, {"name": "b", "persistence": "postgresql", "entities": [{"name": "Thing", "fields": [{"name": "y", "type": "str"}]}]}])
        second = project / "alembic" / "versions" / "9999_second_head.py"
        second.write_text('"""a second root: two heads"""\nrevision = "9999_second_head"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n')
        snapshot = tree(project)
        blocked("heads.json", "adopted_alembic_resolvable", add_modules=[{"name": "labels", "persistence": "postgresql", "entities": entity}])
        second.unlink()
        snapshot = tree(project)
        # the in-memory variant of the same module is still possible on this project
        self.assertEqual(self.plan(project, "memory.json", add_modules=[{"name": "labels", "entities": entity}]).state, "planned")
        # adoption refusals: env.py that targets another metadata, a session dependency that does not resolve, a script location that is not the ini's, persistence allowed without alembic
        for label, mutate, over, fragment in (
            ("other metadata", lambda p: (p / "alembic" / "env.py").write_text((p / "alembic" / "env.py").read_text().replace("target_metadata = Base.metadata", "target_metadata = app.models.Note.metadata")), {}, "env.py assigns target_metadata"),
            ("missing session dependency", lambda p: None, {"alembic": {**ALEMBIC, "session_dependency": "app.db:get_sess"}}, "alembic.session_dependency"),
            ("script location mismatch", lambda p: None, {"alembic": {**ALEMBIC, "script_location": "migrations"}}, "alembic.ini points at 'alembic'"),
            ("metadata that is a function", lambda p: None, {"alembic": {**ALEMBIC, "metadata": "app.db:get_session"}}, "not a declarative base"),
        ):
            fresh = self.project_copy(f"fresh_{abs(hash(label)) % 10000}")
            mutate(fresh)
            contract, errors = adoption.validate_adoption(self.adoption_doc(fresh, **over))
            self.assertEqual(errors, [], label)
            before = tree(fresh)
            with self.assertRaises(adoption.AdoptionError, msg=label) as ctx:
                adoption.adopt(contract)
            self.assertTrue(any(fragment in e for e in ctx.exception.errors), (label, ctx.exception.errors))
            self.assertEqual(tree(fresh), before, label)
        doc = self.adoption_doc(project)
        doc.pop("alembic")
        _, errors = adoption.validate_adoption(doc)
        self.assertTrue(any("adoption.alembic: required" in e for e in errors), errors)
        doc["operations"] = ["add_module", "mount_router"]
        contract, errors = adoption.validate_adoption(doc)
        self.assertEqual(errors, [])
        adoption.adopt(contract)
        plan = self.plan(project, "nopersist.json", add_modules=[{"name": "labels", "persistence": "postgresql", "entities": entity}])
        self.assertTrue(any(b.startswith("persistence_unsupported_for_adopted") for b in plan.blockers), plan.blockers)


if __name__ == "__main__":
    unittest.main()
