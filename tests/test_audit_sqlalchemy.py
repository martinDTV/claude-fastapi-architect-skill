"""SQLAlchemy session and engine rules: detection and known non-findings (fixture sa_sessions)."""

from __future__ import annotations

import unittest

from _audit_support import ProjectCase, audit, audit_fixture, by_rule, lines_of


class SaSessionsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("sa_sessions")
        cls.grouped = by_rule(cls.report)

    def test_module_level_session_instances_are_reported_but_factories_are_not(self) -> None:
        self.assertEqual(lines_of(self.report, "SA_SESSION_MODULE_GLOBAL", "app/bad_global.py"), [5, 6])
        self.assertEqual(lines_of(self.report, "SA_SESSION_MODULE_GLOBAL", "app/aliases.py"), [6, 7])
        # sessionmaker / async_sessionmaker / scoped_session globals in app/db.py are factories: silent
        self.assertEqual(lines_of(self.report, "SA_SESSION_MODULE_GLOBAL", "app/db.py"), [])
        # a session created under `if __name__ == "__main__"` is a script, not a shared global
        self.assertNotIn(9, lines_of(self.report, "SA_SESSION_MODULE_GLOBAL", "app/bad_global.py"))
        test_global = [f for f in self.grouped["SA_SESSION_MODULE_GLOBAL"] if f["file"] == "tests/test_db.py"]
        self.assertEqual([(f["environment"], f["confidence"]) for f in test_global], [("test", "medium")])

    def test_factory_resolved_through_import_alias_and_project_facts(self) -> None:
        shared = [f for f in self.grouped["SA_SESSION_MODULE_GLOBAL"] if f["file"] == "app/aliases.py" and f["line"] == 6][0]
        self.assertEqual(shared["symbol"], "app.db.SessionLocal")
        self.assertIn("AsyncSession", shared["message"])
        legacy = [f for f in self.grouped["SA_SESSION_MODULE_GLOBAL"] if f["file"] == "app/aliases.py" and f["line"] == 7][0]
        self.assertEqual(legacy["symbol"], "sqlalchemy.orm.Session")

    def test_session_shared_across_concurrent_tasks(self) -> None:
        items = sorted(self.grouped["SA_SESSION_SHARED_CONCURRENTLY"], key=lambda f: f["line"])
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items],
                         [("app/service.py", 20, "high"), ("app/service.py", 24, "medium")])
        self.assertTrue(all(f["recommended_test"] for f in items))
        self.assertEqual(items[0]["verification"], "needs_runtime_check")
        # one session per task via async_sessionmaker inside each coroutine, and an awaited
        # asyncio.to_thread(db.commit) offload, are not concurrent sharing
        self.assertNotIn(33, [f["line"] for f in items])
        self.assertNotIn(45, [f["line"] for f in items])

    def test_async_session_coroutine_not_awaited(self) -> None:
        items = self.grouped["SA_ASYNC_SESSION_NOT_AWAITED"]
        self.assertEqual(sorted((f["file"], f["line"]) for f in items), [("app/aliases.py", 11), ("app/service.py", 38)])
        self.assertTrue(all(f["confidence"] == "high" and f["severity"] == "high" for f in items))
        # session.add() is synchronous; a sync helper returning the awaitable is not reported
        self.assertNotIn(40, lines_of(self.report, "SA_ASYNC_SESSION_NOT_AWAITED", "app/service.py"))
        self.assertNotIn(54, lines_of(self.report, "SA_ASYNC_SESSION_NOT_AWAITED", "app/service.py"))

    def test_sync_session_io_inside_async_def_with_graded_confidence(self) -> None:
        items = sorted(self.grouped["SA_SYNC_SESSION_IN_ASYNC_DEF"], key=lambda f: f["line"])
        self.assertEqual([(f["line"], f["confidence"]) for f in items], [(44, "high"), (49, "medium"), (50, "low")])
        self.assertIn("legacy Query API", items[1]["message"])
        self.assertIn("missing 'await'", items[2]["message"])
        # the awaited asyncio.to_thread(db.commit) offload at line 45 is fine
        self.assertNotIn(45, [f["line"] for f in items])

    def test_engine_created_per_call_only_in_handlers_or_plain_functions(self) -> None:
        items = self.grouped["SA_ENGINE_CREATED_PER_CALL"]
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items], [("app/service.py", 59, "high")])
        self.assertIn("route handler", items[0]["message"])
        # module-level engines, lru_cache factories and *_engine factory names are not reported

    def test_create_all_reported_in_application_code_not_in_tests(self) -> None:
        items = self.grouped["SA_CREATE_ALL_AT_RUNTIME"]
        self.assertEqual([(f["file"], f["line"]) for f in items], [("app/service.py", 73)])
        self.assertEqual(items[0]["verification"], "needs_runtime_check")

    def test_session_not_closed_versus_closed_handed_over_or_context_managed(self) -> None:
        items = sorted(self.grouped["SA_SESSION_NOT_CLOSED"], key=lambda f: (f["file"], f["line"]))
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items],
                         [("app/db.py", 25, "high"), ("app/service.py", 77, "medium")])
        self.assertIn("yielded", items[0]["message"])
        service_lines = lines_of(self.report, "SA_SESSION_NOT_CLOSED", "app/service.py")
        for ok_line in (84, 91, 96):  # closes_session, hands_over, context_managed
            self.assertNotIn(ok_line, service_lines)
        self.assertEqual(lines_of(self.report, "SA_SESSION_NOT_CLOSED", "app/db.py"), [25])  # get_session closes

    def test_category_selection_limits_the_report(self) -> None:
        report = audit_fixture("sa_sessions", options=audit.AuditOptions(categories={"sqlalchemy"}))
        rules = {f["rule"] for f in report["findings"]}
        self.assertTrue(rules)
        self.assertTrue(all(r.startswith("SA_") for r in rules), rules)
        self.assertEqual(report["config"]["categories"], ["sqlalchemy"])


class SaSessionsInlineTests(ProjectCase):
    def test_correct_factory_usage_has_no_findings(self) -> None:
        self.write("app/db.py", """
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
            from sqlalchemy.orm import scoped_session, sessionmaker

            engine = create_async_engine("sqlite+aiosqlite://")
            SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            Registry = scoped_session(sessionmaker())


            async def get_session():
                async with SessionLocal() as session:
                    yield session


            async def use(session: AsyncSession) -> None:
                await session.execute("select 1")
                await session.commit()
                session.add(object())
            """)
        self.assertEqual(self.rules(), [])

    def test_unknown_receiver_awaited_calls_are_not_reported(self) -> None:
        self.write("app/svc.py", """
            import sqlalchemy

            async def handler(db):
                await db.execute("select 1")
                await db.commit()
            """)
        self.assertEqual(self.findings("SA_SYNC_SESSION_IN_ASYNC_DEF"), [])

    def test_sessionmaker_begin_context_and_async_scoped_session(self) -> None:
        self.write("app/svc.py", """
            import asyncio
            from sqlalchemy.ext.asyncio import async_sessionmaker

            SessionLocal = async_sessionmaker()


            async def work(item_id: int) -> None:
                async with SessionLocal.begin() as session:
                    session.execute("select 1")                  # AsyncSession from Factory.begin(): missing await
                    await session.commit()


            async def fan_out(session_ids: list[int]) -> None:
                await asyncio.gather(*[work(i) for i in session_ids])   # no session shared
            """)
        self.assertEqual([f["line"] for f in self.findings("SA_ASYNC_SESSION_NOT_AWAITED")], [9])
        self.assertEqual(self.findings("SA_SESSION_SHARED_CONCURRENTLY"), [])

    def test_gather_with_starred_comprehension_sharing_a_typed_session(self) -> None:
        self.write("app/svc.py", """
            import asyncio
            from sqlalchemy.ext.asyncio import AsyncSession

            async def load(session: AsyncSession, i: int) -> None: ...

            async def fan_out(session: AsyncSession) -> None:
                await asyncio.gather(*[load(session, i) for i in range(3)])
            """)
        found = self.findings("SA_SESSION_SHARED_CONCURRENTLY")
        self.assertEqual([(f["line"], f["confidence"]) for f in found], [(7, "high")])

    def test_rules_do_not_run_on_alembic_env_or_revisions(self) -> None:
        self.write("alembic/env.py", """
            from alembic import context
            from sqlalchemy import create_engine

            def run() -> None:
                engine = create_engine("sqlite://")
                context.run_migrations()
            """)
        self.assertEqual(self.findings("SA_ENGINE_CREATED_PER_CALL"), [])

    def test_schema_1_2_fields_present_on_new_findings(self) -> None:
        self.write("app/x.py", """
            from sqlalchemy.orm import Session, sessionmaker
            Local = sessionmaker()
            db = Local()
            """)
        finding = self.findings("SA_SESSION_MODULE_GLOBAL")[0]
        for key in ("category", "verification", "recommended_test", "fingerprint", "environment", "suppressed"):
            self.assertIn(key, finding)
        self.assertEqual(finding["category"], "sqlalchemy")
        self.assertEqual(len(finding["fingerprint"]), 16)


if __name__ == "__main__":
    unittest.main()
