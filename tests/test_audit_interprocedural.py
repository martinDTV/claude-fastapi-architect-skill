"""v1.5 interprocedural engine and the rules built on it (fixtures interproc, hexagonal_sessions).

Covers: call-graph statistics and limits, source-root aliases, dependency and caller
propagation of session kinds with evidence, indirect blocking through sync helpers,
nested begin, async lazy loading, retried writes, request-scoped resources and
background-task context, plus the known non-findings (hexagonal ports, tests,
retry/poll loops, eager strategies, idempotency keys)."""

from __future__ import annotations

import json
import unittest

from _audit_support import ProjectCase, audit, audit_fixture, by_rule, lines_of


class CallGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("interproc")
        cls.grouped = by_rule(cls.report)
        cls.inter = cls.report["interprocedural"]

    def test_statistics_are_reported_and_deterministic(self) -> None:
        inter = self.inter
        self.assertTrue(inter["enabled"])
        self.assertEqual(inter["max_depth"], 4)
        self.assertEqual(inter["functions"], 35)
        self.assertEqual(inter["call_sites"], 91)
        res = inter["resolution"]
        self.assertTrue({"project", "external", "dynamic", "not_found", "constructors"} <= set(res))  # 1.3 keys kept; 1.4 adds builtin/object/ambiguous
        self.assertEqual(sum(res.values()), inter["call_sites"])
        self.assertGreaterEqual(res["project"], 9)
        self.assertEqual(inter["dependency_params"], 4)  # db=Depends(get_db) in four routes
        self.assertGreaterEqual(inter["inferred_params"], 5)
        self.assertFalse(inter["truncated"])
        self.assertNotIn("elapsed_ms", inter)  # timing never enters the report
        self.assertEqual(audit.to_json(audit_fixture("interproc")), audit.to_json(self.report))

    def test_source_root_aliases_resolve_app_imports_to_src_app_functions(self) -> None:
        indirect = self.grouped["ASYNC_BLOCKING_CALL_INDIRECT"][0]
        self.assertEqual(indirect["call_path"], ["src.app.routes.report", "src.app.helpers.compute_report", "src.app.helpers.slow_total", "time.sleep"])

    def test_dependency_yield_types_untyped_route_params(self) -> None:
        # service.create(session, ...) receives the route's db (typed only through get_db's yield)
        items = sorted(self.grouped["SA_ASYNC_SESSION_NOT_AWAITED"], key=lambda f: (f["file"], f["line"]))
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["evidence"]) for f in items],
                         [("src/app/helpers.py", 10, "medium", "inferred"), ("src/app/repo.py", 6, "medium", "inferred"), ("src/app/service.py", 8, "medium", "inferred")])
        self.assertIn("inferred from a caller", items[2]["message"])

    def test_method_resolution_through_instance_and_base_class(self) -> None:
        # via_repo -> CachedOrderRepo() instance -> OrderRepo.fetch (base) receives the session
        self.assertEqual(lines_of(self.report, "SA_ASYNC_SESSION_NOT_AWAITED", "src/app/repo.py"), [6])

    def test_commit_in_loop_on_inferred_session_is_medium(self) -> None:
        items = [f for f in self.grouped["TX_COMMIT_IN_LOOP"] if f["file"] == "src/app/service.py"]
        self.assertEqual([(f["line"], f["confidence"], f["evidence"]) for f in items], [(16, "medium", "inferred")])
        # retry (for attempt in range) and poll (while ... try) loops in retry.py are not per-item commits
        self.assertEqual(lines_of(self.report, "TX_COMMIT_IN_LOOP", "src/app/retry.py"), [])

    def test_indirect_blocking_through_sync_helper_with_call_path(self) -> None:
        items = self.grouped["ASYNC_BLOCKING_CALL_INDIRECT"]
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["evidence"], f["symbol"]) for f in items],
                         [("src/app/routes.py", 32, "medium", "verified", "time.sleep")])
        self.assertIn("2 resolved calls", items[0]["message"])
        # the async test calling the same helper is not reported (tests use sync helpers freely)
        self.assertEqual([f["rule"] for f in self.report["findings"] if f["file"].startswith("tests/")], ["ASYNC_BLOCKING_CALL"])

    def test_call_depth_limits_indirect_findings(self) -> None:
        one = audit_fixture("interproc", options=audit.AuditOptions(max_call_depth=1))
        two = audit_fixture("interproc", options=audit.AuditOptions(max_call_depth=2))
        self.assertEqual(lines_of(one, "ASYNC_BLOCKING_CALL_INDIRECT"), [])
        self.assertEqual(lines_of(two, "ASYNC_BLOCKING_CALL_INDIRECT"), [32])
        self.assertEqual(one["interprocedural"]["max_depth"], 1)

    def test_disabling_the_pass_falls_back_to_v14_behaviour(self) -> None:
        report = audit_fixture("interproc", options=audit.AuditOptions(interprocedural=False))
        self.assertEqual(report["interprocedural"], {"enabled": False, "max_depth": 4})
        rules = report["summary"]["by_rule"]
        for rule in ("ASYNC_BLOCKING_CALL_INDIRECT", "SA_ASYNC_SESSION_NOT_AWAITED", "RETRY_NON_IDEMPOTENT_WRITE", "BG_REQUEST_SCOPED_RESOURCE", "BG_TASK_CONTEXT_UNVERIFIED"):
            self.assertNotIn(rule, rules)
        self.assertIn("TX_NESTED_BEGIN", rules)  # intraprocedural rules still run
        # without kinds the untyped session in service.create degrades to the name-based low-confidence signal
        self.assertIn("SA_SYNC_SESSION_IN_ASYNC_DEF", rules)


class NewRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("interproc")
        cls.grouped = by_rule(cls.report)

    def test_nested_begin_versus_savepoint_and_sequential(self) -> None:
        items = self.grouped["TX_NESTED_BEGIN"]
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items], [("src/app/tx.py", 9, "high")])
        self.assertIn("begin_nested", items[0]["message"])

    def test_async_lazy_load_on_implicit_relationships_only(self) -> None:
        items = sorted(self.grouped["SA_ASYNC_LAZY_LOAD_RISK"], key=lambda f: f["line"])
        self.assertEqual([(f["line"], f["symbol"], f["confidence"], f["verification"]) for f in items],
                         [(10, "Order.items", "medium", "needs_runtime_check"), (33, "Order.items", "medium", "needs_runtime_check")])
        self.assertIn("MissingGreenlet", items[0]["message"])
        self.assertIn("MissingGreenlet", items[0]["recommended_test"])
        # lazy="selectin" (line 15), lazy="raise" (20), explicit options (26) and the sync function (39) are silent
        self.assertEqual(lines_of(self.report, "SA_ASYNC_LAZY_LOAD_RISK"), [10, 33])

    def test_retried_writes_without_idempotency_signal(self) -> None:
        items = sorted(self.grouped["RETRY_NON_IDEMPOTENT_WRITE"], key=lambda f: f["line"])
        self.assertEqual([(f["line"], f["confidence"], f["evidence"]) for f in items], [(11, "medium", "verified"), (25, "medium", "verified")])
        self.assertIn("retry decorator", items[0]["message"])
        self.assertIn("hand-written retry loop", items[1]["message"])
        self.assertIn("exactly one effect", items[0]["recommended_test"])
        # idempotency key, read-only retry and the poll loop are silent

    def test_request_scoped_session_handed_to_background_task(self) -> None:
        items = self.grouped["BG_REQUEST_SCOPED_RESOURCE"]
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["evidence"]) for f in items], [("src/app/routes.py", 17, "high", "verified")])
        self.assertIn("'db' (AsyncSession)", items[0]["message"])

    def test_background_context_classification(self) -> None:
        items = self.grouped["BG_TASK_CONTEXT_UNVERIFIED"]
        self.assertEqual([(f["file"], f["line"], f["severity"], f["evidence"], f["verification"]) for f in items],
                         [("src/app/routes.py", 18, "info", "unknown", "not_verifiable_statically")])
        self.assertEqual(items[0]["call_path"], ["create_order", "src.app.tasks.send_email"])
        # reindex (recreates a session and sets tenant context), send_receipt (principal captured)
        # and audit_log via create_task (own session) are not reported
        self.assertEqual(lines_of(self.report, "BG_TASK_CONTEXT_UNVERIFIED"), [18])

    def test_schema_1_3_fields(self) -> None:
        self.assertGreaterEqual(tuple(int(x) for x in self.report["schema_version"].split(".")), (1, 3))
        for finding in self.report["findings"]:
            self.assertIn(finding["evidence"], {"verified", "inferred", "unknown"})
            self.assertIn("call_path", finding)
        self.assertIn("interprocedural", self.report)
        self.assertIn("reliability", self.report["summary"]["by_category"])


class HexagonalNoFalsePositiveTests(unittest.TestCase):
    def test_ports_and_unit_of_work_produce_no_findings(self) -> None:
        report = audit_fixture("hexagonal_sessions")
        # ports, adapters and the unit of work raise nothing. The one finding left is a security question, not a
        # session or transaction one: since the v2.0 security hardening a router-level authentication dependency
        # (`APIRouter(dependencies=[Depends(current_user)])`) is no longer counted as an authorization kind, and
        # `post_order` acts on `order_id` through a service call that never receives a principal
        self.assertEqual([(f["rule"], f["function"], f["confidence"], f["evidence_state"]) for f in report["findings"]], [("SEC_RESOURCE_AUTHZ_UNVERIFIED", "post_order", "low", "insufficient")])
        self.assertGreaterEqual(report["interprocedural"]["functions"], 10)


class InlineInterproceduralTests(ProjectCase):
    def test_depends_with_return_annotation_types_the_parameter(self) -> None:
        self.write("app/db.py", """
            from typing import AsyncIterator
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            Local = async_sessionmaker()

            async def get_session() -> AsyncIterator[AsyncSession]:
                async with Local() as s:
                    yield s
            """)
        self.write("app/routes.py", """
            from fastapi import APIRouter, Depends
            from app.db import get_session

            router = APIRouter()

            @router.get("/x")
            async def x(session=Depends(get_session)) -> dict:
                session.execute("select 1")
                return {}
            """)
        found = self.findings("SA_ASYNC_SESSION_NOT_AWAITED")
        self.assertEqual([(f["line"], f["confidence"], f["evidence"]) for f in found], [(8, "high", "verified")])

    def test_caller_propagation_two_levels_deep(self) -> None:
        self.write("app/a.py", """
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.b import middle

            async def top(session: AsyncSession):
                await middle(session)
            """)
        self.write("app/b.py", """
            from app.c import leaf

            async def middle(s):
                await leaf(s)
            """)
        self.write("app/c.py", """
            async def leaf(db):
                db.commit()
            """)
        found = self.findings("SA_ASYNC_SESSION_NOT_AWAITED")
        self.assertEqual([(f["file"], f["line"], f["evidence"]) for f in found], [("app/c.py", 2, "inferred")])

    def test_declared_annotation_wins_over_inferred_kind(self) -> None:
        self.write("app/a.py", """
            from sqlalchemy.orm import Session
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.b import work

            async def top(session: AsyncSession):
                await work(session)
            """)
        self.write("app/b.py", """
            from sqlalchemy.orm import Session

            async def work(session: Session):
                session.commit()           # declared sync Session: blocking, not missing await
            """)
        self.assertEqual([f["rule"] for f in self.findings() if f["file"] == "app/b.py"], ["SA_SYNC_SESSION_IN_ASYNC_DEF"])

    def test_cycle_and_unresolved_calls_do_not_break_the_graph(self) -> None:
        self.write("app/a.py", "from app.b import g\n\ndef f(x):\n    return g(x)\n")
        self.write("app/b.py", "from app.a import f\n\ndef g(x):\n    return f(x) + unknown(x) + x.method()\n")
        report = self.run_audit()
        inter = report["interprocedural"]
        self.assertEqual(inter["resolution"]["project"], 2)
        self.assertGreaterEqual(inter["resolution"]["dynamic"], 1)
        self.assertFalse(inter["truncated"])

    def test_time_budget_truncation_is_reported_not_fatal(self) -> None:
        for i in range(30):
            self.write(f"pkg/m{i}.py", f"from pkg.m{(i + 1) % 30} import f{(i + 1) % 30}\n\ndef f{i}(x):\n    return f{(i + 1) % 30}(x)\n")
        report = self.run_audit(options=audit.AuditOptions(time_budget_s=0.0))
        self.assertTrue(report["interprocedural"]["truncated"])
        self.assertIn("time budget", report["interprocedural"]["truncated_reason"])

    def test_cli_flags(self) -> None:
        self.write("app/x.py", "import time\n\ndef helper():\n    time.sleep(1)\n\nasync def route():\n    helper()\n")
        default = json.loads(self.cli(str(self.root), "--json").stdout)
        self.assertEqual([f["rule"] for f in default["findings"]], ["ASYNC_BLOCKING_CALL_INDIRECT"])
        off = json.loads(self.cli(str(self.root), "--json", "--no-interprocedural").stdout)
        self.assertEqual(off["findings"], [])
        shallow = json.loads(self.cli(str(self.root), "--json", "--max-call-depth", "1").stdout)
        self.assertEqual([f["rule"] for f in shallow["findings"]], ["ASYNC_BLOCKING_CALL_INDIRECT"])  # direct callee blocks
        text = self.cli(str(self.root)).stdout
        self.assertIn("call graph:", text)
        self.assertIn("call path:", text)


if __name__ == "__main__":
    unittest.main()
