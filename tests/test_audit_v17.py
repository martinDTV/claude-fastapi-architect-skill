"""v1.7: security contracts per route, declared-public routes, resource authorization and RLS-context
questions, coroutine/lock/HTTP-client lifecycle rules, the independent dev corpus scored in-process
(the acceptance partition is held out and scored only by scripts/audit_precision.py at release time),
and schema 1.5 contracts."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS, ProjectCase, audit, audit_fixture, by_rule, lines_of


def _load_precision():
    spec = importlib.util.spec_from_file_location("audit_precision_v17", REPO_ROOT / "scripts" / "audit_precision.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


precision = _load_precision()
DEV = FIXTURES / "truth17" / "dev"


class DevCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.truth = precision.load_truth(str(DEV))
        cls.score = precision.score(cls.truth, precision.collect_run(str(DEV)))

    def test_expectations_were_written_independently_and_all_hold(self) -> None:
        self.assertGreaterEqual(len(self.truth["expected"]), 20)
        self.assertGreaterEqual(len(self.truth["absent"]), 40)
        self.assertEqual(self.score["false_negatives"], [])
        self.assertEqual(self.score["false_positives"], [])
        self.assertEqual(self.score["evidence_mismatches"], [])
        self.assertEqual((self.score["precision"], self.score["recall"]), (1.0, 1.0))
        self.assertEqual(self.score["false_positive_rate"], 0.0)

    def test_questions_carry_unknown_evidence_and_the_rest_is_verified(self) -> None:
        counts = self.score["evidence_counts"]
        self.assertEqual(counts["unknown"], 3)  # resource authorization, privilege change, tenant binding: questions
        self.assertGreaterEqual(counts["verified"], 15)
        self.assertLessEqual(self.score["unknown_rate"], 0.2)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("truth17/dev")
        cls.routes = {c["function"]: c for c in cls.report["contracts"]["routes"]}

    def test_contract_block_is_additive_and_summarised(self) -> None:
        self.assertEqual(self.report["schema_version"], "1.5")
        summary = self.report["contracts"]["summary"]
        self.assertEqual(summary["routes"], len(self.report["contracts"]["routes"]))
        self.assertEqual(summary["unauthenticated_not_public"], 1)  # /orders
        self.assertEqual(summary["public"], 3)
        self.assertIn("role", self.report["contracts"]["authorization_kinds"])

    def test_public_routes_by_path_config_and_marker(self) -> None:
        self.assertEqual(self.routes["health"]["public_source"], "path")
        self.assertEqual(self.routes["provider_webhook"]["public_source"], "config")
        self.assertEqual(self.routes["pricing"]["public_source"], "marker")
        self.assertFalse(self.routes["orders"]["public"])
        self.assertEqual(self.routes["orders"]["authentication"], "absent")

    def test_authentication_through_chains_classes_scopes_and_router_dependencies(self) -> None:
        self.assertEqual(self.routes["me"]["authentication"], "present")
        self.assertIn("get_audit_context -> get_context -> get_current_user", self.routes["me"]["authentication_evidence"][0])
        self.assertIn("role", self.routes["admin_stats"]["authorization"])
        self.assertIn("scopes", self.routes["reports"]["authorization"])
        self.assertIn("router_dependencies", self.routes["queues"]["authorization"])
        self.assertIn("role", self.routes["delete_account"]["authorization"])  # require("accounts:delete")

    def test_resource_relation_kinds(self) -> None:
        self.assertIn("principal_with_resource", self.routes["freeze_account"]["authorization"])
        self.assertIn("authorization_call", self.routes["archive_account"]["authorization"])
        self.assertIn("authorization_call", self.routes["tag_account"]["authorization"])
        self.assertEqual(self.routes["close_account"]["authorization"], [])
        self.assertEqual(self.routes["close_account"]["evidence"], "unknown")
        self.assertEqual(self.routes["close_account"]["resource_params"], ["account_id"])
        self.assertIn("membership", self.routes["invoices"]["authorization"])
        self.assertIn("tenant_comparison", self.routes["summary"]["authorization"])
        self.assertIn("decorator", self.routes["set_role_decorated"]["authorization"])


class NewRuleTests(ProjectCase):
    def test_rls_context_from_request_replaces_the_generic_question(self) -> None:
        report = audit_fixture("truth17/dev")
        rls = by_rule(report)["SEC_RLS_CONTEXT_FROM_REQUEST"]
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["evidence"]) for f in rls], [("app/routes/tenancy.py", 20, "medium", "verified")])
        self.assertEqual(rls[0]["call_path"], ["export", "set_tenant_context", "set_config"])
        self.assertEqual(lines_of(report, "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL", "app/routes/tenancy.py"), [40])

    def test_resource_authorization_question_message_and_evidence(self) -> None:
        report = audit_fixture("truth17/dev")
        items = by_rule(report)["SEC_RESOURCE_AUTHZ_UNVERIFIED"]
        self.assertEqual([(f["line"], f["confidence"], f["evidence"], f["verification"]) for f in items], [(58, "low", "unknown", "not_verifiable_statically")])
        self.assertIn("app.services.accounts.close(59)", items[0]["message"])
        self.assertIn("not related to the resource", items[0]["recommended_test"])

    def test_coroutine_lock_and_client_lifecycle(self) -> None:
        report = audit_fixture("truth17/dev")
        self.assertEqual(lines_of(report, "ASYNC_COROUTINE_NOT_AWAITED", "app/routes/resources.py"), [21, 27])
        dropped = by_rule(report)["ASYNC_COROUTINE_NOT_AWAITED"][0]
        self.assertEqual(dropped["call_path"], ["summary", "app.services.accounts.compute_summary"])
        self.assertEqual(lines_of(report, "ASYNC_LOCK_PER_CALL", "app/routes/resources.py"), [52])
        self.assertEqual(lines_of(report, "REL_HTTP_CLIENT_PER_CALL", "app/routes/resources.py"), [66])

    def test_coroutine_not_awaited_through_an_instance_attribute(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/audit.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession

            class AuditLog:
                def __init__(self, db: AsyncSession) -> None:
                    self._db = db

                async def record(self, action: str) -> None:
                    await self._db.execute(action)

                async def twice(self) -> None:
                    self.record("a")
                    await self.record("b")
        ''')
        items = self.findings("ASYNC_COROUTINE_NOT_AWAITED")
        self.assertEqual([(f["line"], f["evidence"]) for f in items], [(11, "verified")])

    def test_shared_session_rule_accepts_a_task_awaited_immediately(self) -> None:
        self.write("app/svc.py", '''
            import asyncio
            from sqlalchemy.ext.asyncio import AsyncSession

            async def work(session: AsyncSession) -> int:
                return 1

            async def run(session: AsyncSession) -> int:
                task = asyncio.create_task(work(session))
                return await task

            async def overlap(session: AsyncSession) -> int:
                task = asyncio.create_task(work(session))
                await session.execute("select 1")       # the creator keeps using the session
                return await task
        ''')
        self.assertEqual(lines_of(self.run_audit(), "SA_SESSION_SHARED_CONCURRENTLY"), [12])

    def test_tenant_params_are_configurable(self) -> None:
        self.write(".audit-fastapi.json", '{"tenant_params": ["clinic_id"], "public_routes": ["/open/*"]}')
        self.write("app/r.py", '''
            from uuid import UUID
            from fastapi import APIRouter, Depends

            router = APIRouter()

            async def current_staff():
                return {}

            @router.get("/clinics/{clinic_id}/records")
            async def records(clinic_id: UUID, staff=Depends(current_staff)) -> list:
                return await load(clinic_id)

            @router.get("/open/schedule")
            async def schedule() -> dict:
                return {}

            async def load(clinic_id: UUID) -> list:
                return []
        ''')
        report = self.run_audit()
        self.assertEqual(lines_of(report, "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL"), [10])
        self.assertEqual(lines_of(report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"), [])
        self.assertEqual({c["function"]: c["public_source"] for c in report["contracts"]["routes"]}["schedule"], "config")

    def test_collection_endpoints_on_prefixed_routers_are_not_public(self) -> None:
        self.write("app/r.py", '''
            from fastapi import APIRouter

            router = APIRouter(prefix="/orders")

            @router.get("")
            async def list_orders() -> list:
                return []
        ''')
        self.assertEqual(lines_of(self.run_audit(), "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"), [6])


class CompatibilityTests(ProjectCase):
    def test_rule_count_and_schema_versions(self) -> None:
        for rule in ("SEC_RESOURCE_AUTHZ_UNVERIFIED", "SEC_RLS_CONTEXT_FROM_REQUEST", "ASYNC_COROUTINE_NOT_AWAITED", "ASYNC_LOCK_PER_CALL", "REL_HTTP_CLIENT_PER_CALL"):
            self.assertIn(rule, audit.RULES)
        self.assertEqual(len(audit.RULES), 53)
        self.assertEqual(audit.SCHEMA_VERSION, "1.5")
        self.assertEqual(audit.TOOL_VERSION, "1.7.0")

    def test_report_without_routes_has_an_empty_contract_block(self) -> None:
        self.write("app/m.py", "def f():\n    return 1\n")
        report = self.run_audit()
        self.assertEqual(report["contracts"]["summary"]["routes"], 0)
        self.assertEqual(report["contracts"]["routes"], [])

    def test_cli_json_carries_contracts_and_is_deterministic(self) -> None:
        self.write("app/r.py", "from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.get('/health')\nasync def h() -> dict:\n    return {}\n")
        a = self.cli(".", "--format", "json").stdout
        b = self.cli(".", "--format", "json").stdout
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a)["contracts"]["summary"]["public"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
