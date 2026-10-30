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
        self.assertEqual(counts["unknown"], 4)  # resource authorization, privilege change, tenant binding, consumer: questions
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
        self.assertEqual(summary["public"], 2)  # declared only; /health is exposure_undetermined
        self.assertEqual(summary["exposure_undetermined"], 1)
        self.assertIn("role", self.report["contracts"]["authorization_kinds"])

    def test_public_routes_by_path_config_and_marker(self) -> None:
        self.assertEqual(self.routes["health"]["public_source"], "path")
        self.assertFalse(self.routes["health"]["public"])  # a convention is recorded, never a declaration
        self.assertEqual(self.routes["health"]["bucket"], "exposure_undetermined")
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
        for rule in ("SEC_RESOURCE_AUTHZ_UNVERIFIED", "SEC_RLS_CONTEXT_FROM_REQUEST", "ASYNC_COROUTINE_NOT_AWAITED", "ASYNC_LOCK_PER_CALL", "REL_HTTP_CLIENT_PER_CALL",
                     "CONTRACT_ROUTE_AUTH_REQUIRED", "CONTRACT_MUTATION_AUTHZ_REQUIRED", "CONTRACT_IMPORT_FORBIDDEN", "CONTRACT_COMMIT_OUTSIDE_LAYER", "REL_CONSUMER_WITHOUT_INBOX"):
            self.assertIn(rule, audit.RULES)
        self.assertEqual(len(audit.RULES), 58)
        self.assertIn("contracts", audit.CATEGORIES)
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
        summary = json.loads(a)["contracts"]["summary"]
        self.assertEqual((summary["public"], summary["exposure_undetermined"]), (0, 1))  # /health is a convention, not a declaration


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class EvidenceStateTests(ProjectCase):
    def test_every_finding_carries_one_of_the_six_states_and_the_summary_counts_them(self) -> None:
        self.write("app/a.py", '''
            import time
            import requests

            async def h():
                time.sleep(1)  # noqa: ASYNC210
                requests.get("https://x.invalid")
        ''')
        self.write("app/broken.py", "def (:\n")
        report = self.run_audit()
        states = {f["rule"]: f["evidence_state"] for f in report["findings"]}
        self.assertEqual(states["PY_SYNTAX"], "partial")
        self.assertEqual(states["REL_HTTP_CALL_WITHOUT_TIMEOUT"], "static_confirmed")
        blocking = [f for f in report["findings"] if f["rule"] == "ASYNC_BLOCKING_CALL"]
        self.assertEqual(sorted(f["evidence_state"] for f in blocking), ["static_confirmed", "suppressed"])
        by_state = report["summary"]["by_evidence_state"]
        self.assertEqual(set(by_state), {"reproduced", "static_confirmed", "inferred", "insufficient", "partial", "suppressed"})
        self.assertEqual(by_state["reproduced"], 0)  # static analysis never claims it
        self.assertEqual(sum(by_state.values()), len(report["findings"]))

    def test_questions_are_insufficient_and_propagated_kinds_are_inferred(self) -> None:
        report = audit_fixture("truth17/dev")
        states = {(f["rule"], f["evidence_state"]) for f in report["findings"]}
        self.assertIn(("SEC_RESOURCE_AUTHZ_UNVERIFIED", "insufficient"), states)
        self.assertIn(("SEC_RLS_CONTEXT_FROM_REQUEST", "static_confirmed"), states)
        inter = audit_fixture("interproc")
        self.assertIn(("SA_ASYNC_SESSION_NOT_AWAITED", "inferred"), {(f["rule"], f["evidence_state"]) for f in inter["findings"]})


class ContractPartitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("truth17/dev")
        cls.by_function = {(c["file"], c["function"]): c for c in cls.report["contracts"]["routes"]}

    def test_partition_is_disjoint_and_complete(self) -> None:
        partition = self.report["contracts"]["partition"]
        self.assertEqual(sum(partition.values()), self.report["contracts"]["summary"]["routes"])
        self.assertEqual(set(partition), {"public_declared", "exposure_undetermined", "unauthenticated_not_public", "optional_authentication", "authenticated_authorization_visible", "authenticated_only", "authenticated_authorization_unknown"})
        self.assertEqual(partition["public_declared"], 2)  # webhook (config) and pricing (marker)
        self.assertEqual(partition["exposure_undetermined"], 1)  # /health by naming convention only
        self.assertEqual(partition["unauthenticated_not_public"], 1)

    def test_inherited_router_dependencies_and_resource_kinds(self) -> None:
        queues = self.by_function[("app/routes/contracts.py", "queues")]
        self.assertEqual(queues["authentication"], "present")
        self.assertIn("router_dependencies", queues["authorization"])
        self.assertEqual(queues["bucket"], "authenticated_authorization_visible")
        self.assertEqual(self.by_function[("app/routes/contracts.py", "close_account")]["bucket"], "authenticated_authorization_unknown")
        self.assertEqual(self.by_function[("app/routes/contracts.py", "me")]["bucket"], "authenticated_only")
        self.assertEqual(self.by_function[("app/routes/contracts.py", "freeze_account")]["bucket"], "authenticated_authorization_visible")


class OptionalAuthenticationTests(ProjectCase):
    def test_optional_dependency_is_its_own_category_and_not_an_unauthenticated_route(self) -> None:
        self.write("app/r.py", '''
            from fastapi import APIRouter, Depends, Header

            router = APIRouter()

            async def bearer(authorization: str = Header(default="")) -> str:
                return authorization

            async def optional_user(token: str = Depends(bearer)) -> dict | None:
                return None if not token else {"id": 1}

            @router.get("/preview/{item_id}")
            async def preview(item_id: int, user: dict | None = Depends(optional_user)) -> dict:
                return {"item": item_id, "anonymous": user is None}
        ''')
        report = self.run_audit()
        contract = report["contracts"]["routes"][0]
        self.assertEqual(contract["authentication"], "optional")
        self.assertEqual(contract["bucket"], "optional_authentication")
        self.assertEqual(lines_of(report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"), [])
        self.assertEqual(lines_of(report, "SEC_RESOURCE_AUTHZ_UNVERIFIED"), [])  # anonymous access is the design; nothing to relate


class DeclaredContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("contracts_declared")
        cls.grouped = by_rule(cls.report)

    def test_declared_contracts_fire_only_where_declared(self) -> None:
        self.assertEqual(lines_of(self.report, "CONTRACT_ROUTE_AUTH_REQUIRED"), [30])  # /api/orders without authentication
        self.assertEqual(lines_of(self.report, "CONTRACT_MUTATION_AUTHZ_REQUIRED"), [35])  # POST /api/orders authenticated only
        self.assertEqual(lines_of(self.report, "CONTRACT_IMPORT_FORBIDDEN"), [6])  # shop.api -> shop.infra
        self.assertEqual(lines_of(self.report, "CONTRACT_COMMIT_OUTSIDE_LAYER", "shop/api/orders.py"), [43])  # commit in a router file
        self.assertEqual(lines_of(self.report, "CONTRACT_COMMIT_OUTSIDE_LAYER", "shop/services/orders.py"), [])
        for f in self.grouped["CONTRACT_IMPORT_FORBIDDEN"]:
            self.assertIn("contracts.forbidden_imports", f["message"])
            self.assertEqual(f["category"], "contracts")
            self.assertEqual(f["evidence_state"], "static_confirmed")

    def test_without_a_declaration_nothing_fires(self) -> None:
        report = audit_fixture("truth16")
        self.assertFalse(any(f["rule"].startswith("CONTRACT_") for f in report["findings"]))


class ConfigIsDataTests(ProjectCase):
    def test_project_python_is_never_imported_or_executed(self) -> None:
        marker = self.root / "EXECUTED"
        self.write("conftest.py", f"open({str(marker)!r}, 'w').write('x')\n")
        self.write("sitecustomize.py", f"open({str(marker)!r}, 'w').write('x')\n")
        self.write("app/__init__.py", f"open({str(marker)!r}, 'w').write('x')\n")
        self.write(".audit-fastapi.json", '{"contracts": {"commit_layers": ["service"]}, "public_routes": ["/x"]}')
        self.run_audit()
        self.cli(".", "--format", "sarif")
        self.assertFalse(marker.exists(), "the auditor executed project Python")

    def test_malformed_contract_declarations_fail_closed_without_executing_anything(self) -> None:
        self.write(".audit-fastapi.json", '{"contracts": {"forbidden_imports": ["__import__(\'os\')"], "route_auth_required": 42, "commit_layers": [1]}}')
        self.write("app/a.py", "import time\nasync def h():\n    time.sleep(1)\n")
        with self.assertRaises(audit.ConfigError) as caught:
            self.run_audit()
        self.assertGreaterEqual(len(caught.exception.errors), 3)
        proc = self.cli(".", "--format", "json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")  # no report that looks complete
        self.assertIn("contracts.route_auth_required", proc.stderr)


class SarifTests(ProjectCase):
    def _report_and_sarif(self):
        self.write("app/a.py", '''
            import time
            import requests

            async def h():
                time.sleep(1)  # audit-fastapi: ignore[ASYNC_BLOCKING_CALL]
                requests.get("https://x.invalid")
        ''')
        proc = self.cli(".", "--format", "sarif")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_document_has_the_required_sarif_2_1_0_structure(self) -> None:
        doc = self._report_and_sarif()
        self.assertEqual(doc["version"], "2.1.0")
        self.assertIn("sarif-schema-2.1.0.json", doc["$schema"])
        run = doc["runs"][0]
        driver = run["tool"]["driver"]
        self.assertEqual(driver["name"], "audit_fastapi")
        self.assertEqual(driver["version"], audit.TOOL_VERSION)
        for rule in driver["rules"]:
            self.assertTrue(set(rule) >= {"id", "shortDescription", "fullDescription", "defaultConfiguration", "helpUri"})
            self.assertIn(rule["defaultConfiguration"]["level"], {"error", "warning", "note"})
        self.assertEqual(run["originalUriBaseIds"]["%SRCROOT%"]["uri"], "file://" + str(self.root) + "/")
        for result in run["results"]:
            self.assertIn(result["ruleId"], {r["id"] for r in driver["rules"]})
            self.assertEqual(driver["rules"][result["ruleIndex"]]["id"], result["ruleId"])
            self.assertIn(result["level"], {"error", "warning", "note"})
            loc = result["locations"][0]["physicalLocation"]
            self.assertEqual(loc["artifactLocation"]["uriBaseId"], "%SRCROOT%")
            self.assertGreaterEqual(loc["region"]["startLine"], 1)
            self.assertGreaterEqual(loc["region"]["startColumn"], 1)
            self.assertIn("fastapiArchitect/fingerprint/v1", result["partialFingerprints"])
            self.assertIn(result["properties"]["evidence_state"], {"static_confirmed", "inferred", "insufficient", "partial", "suppressed"})
        self.assertTrue(run["invocations"][0]["executionSuccessful"])

    def test_suppressions_fingerprints_and_levels_match_the_json_report(self) -> None:
        doc = self._report_and_sarif()
        report = json.loads(self.cli(".", "--format", "json").stdout)
        by_fp = {f["fingerprint"]: f for f in report["findings"]}
        results = doc["runs"][0]["results"]
        self.assertEqual(len(results), len(report["findings"]))
        for result in results:
            f = by_fp[result["partialFingerprints"]["fastapiArchitect/fingerprint/v1"]]
            self.assertEqual(result["level"], {"high": "error", "medium": "warning"}.get(f["severity"], "note"))
            if f["suppressed"]:
                self.assertEqual(result["suppressions"][0]["kind"], "inSource")
                self.assertIn("ASYNC_BLOCKING_CALL", result["suppressions"][0]["justification"])
            else:
                self.assertNotIn("suppressions", result)

    def test_sarif_file_option_keeps_the_other_formats_and_is_deterministic(self) -> None:
        self._report_and_sarif()
        out = self.root / "audit.sarif"
        proc = self.cli(".", "--sarif", str(out), "--format", "markdown")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("# FastAPI static audit"))
        first = out.read_text()
        self.cli(".", "--sarif", str(out), "--format", "json")
        self.assertEqual(first, out.read_text())
        self.assertEqual(json.loads(first)["version"], "2.1.0")


class ConsumerInboxTests(ProjectCase):
    def test_consumer_question_and_its_negatives(self) -> None:
        report = audit_fixture("truth17/dev")
        items = by_rule(report)["REL_CONSUMER_WITHOUT_INBOX"]
        self.assertEqual([(f["file"], f["line"], f["evidence"], f["evidence_state"]) for f in items], [("app/services/consumers.py", 7, "unknown", "insufficient")])
        self.assertEqual(lines_of(report, "REL_CONSUMER_WITHOUT_INBOX", "app/services/reliability.py"), [])  # consume() has an inbox


class ConfigValidationTests(ProjectCase):
    """The contracts section fails closed; everything older keeps its lenient behaviour."""

    def _ok(self, config: str) -> None:
        self.write(".audit-fastapi.json", config)
        self.write("app/a.py", "import time\nasync def h():\n    time.sleep(1)\n")
        report = self.run_audit()
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL"), [3])

    def _bad(self, config: str, *fragments: str) -> None:
        self.write(".audit-fastapi.json", config)
        self.write("app/a.py", "import time\nasync def h():\n    time.sleep(1)\n")
        with self.assertRaises(audit.ConfigError) as caught:
            self.run_audit()
        joined = " ".join(caught.exception.errors)
        for fragment in fragments:
            self.assertIn(fragment, joined)
        proc = self.cli(".", "--format", "json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn(".audit-fastapi.json", proc.stderr)

    def test_valid_declarations_pass(self) -> None:
        self._ok('{"public_routes": ["/public/*"], "tenant_params": ["clinic_id"], "contracts": {"route_auth_required": ["/api/*"], "mutation_authorization_required": ["/api/*"], "forbidden_imports": [{"from": "app.api", "to": "app.infra", "x-reason": "ADR 12"}], "commit_layers": ["service", "custom"], "x-owner": "platform"}, "layers": {"custom": ["*/custom/*"]}}')

    def test_absent_contracts_keep_the_previous_behaviour(self) -> None:
        self._ok('{"layers": {"service": ["*/svc/*"]}, "exclude": ["vendor"], "ignore_rules": ["ROUTE_NO_RETURN_ANNOTATION"], "unknown_key": {"anything": 1}}')
        self._ok('{}')

    def test_wrong_types_and_unknown_properties(self) -> None:
        self._bad('{"contracts": []}', "contracts: expected an object")
        self._bad('{"contracts": {"route_auth_required": "/api/*"}}', "contracts.route_auth_required: expected a list")
        self._bad('{"contracts": {"route_auth_required": ["/api/*"], "forbid": []}}', "contracts.forbid: unknown property")
        self._bad('{"contracts": {}}', "declares nothing")
        self._bad('{"tenant_params": "clinic_id"}', "tenant_params: expected a list")
        self._bad('{"public_routes": [1]}', "public_routes[0]: expected a string")

    def test_invalid_globs_and_patterns(self) -> None:
        self._bad('{"contracts": {"route_auth_required": ["api/*"]}}', "route globs must start with '/'")
        self._bad('{"contracts": {"route_auth_required": ["/api/[a"]}}', "unbalanced brackets")
        self._bad('{"contracts": {"route_auth_required": [" /api/*"]}}', "whitespace")
        self._bad('{"contracts": {"route_auth_required": []}}', "must not be empty")

    def test_required_fields_and_incompatible_rules(self) -> None:
        self._bad('{"contracts": {"forbidden_imports": [{"from": "app.api"}]}}', "missing required property 'to'")
        self._bad('{"contracts": {"forbidden_imports": [{"from": "app.api", "to": "app.api"}]}}', "same module")
        self._bad('{"contracts": {"forbidden_imports": [{"from": "app", "to": "app.infra"}]}}', "nest one another")
        self._bad('{"contracts": {"forbidden_imports": [{"from": "app.api", "to": "os.path", "via": 1}]}}', "unknown property")
        self._bad('{"contracts": {"commit_layers": ["svc"]}}', "unknown layer 'svc'")
        self._bad('{"public_routes": ["/api/*"], "contracts": {"route_auth_required": ["/api/*"]}}', "also listed in public_routes")

    def test_errors_carry_the_configuration_location(self) -> None:
        self.write("custom.json", '{"contracts": {"commit_layers": [1]}}')
        self.write("app/a.py", "x = 1\n")
        proc = self.cli(".", "--config", "custom.json", "--format", "json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("custom.json", proc.stderr)
        self.assertIn("contracts.commit_layers[0]", proc.stderr)


class RegressionV17Tests(unittest.TestCase):
    """Specific fixtures for the defects corrected in the third review (the held-out corpora are not re-scored)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("regressions_v17")
        cls.routes = {c["function"]: c for c in cls.report["contracts"]["routes"]}

    def test_health_convention_never_overrides_a_declared_contract(self) -> None:
        self.assertEqual(self.routes["status"]["bucket"], "exposure_undetermined")
        self.assertEqual(self.routes["status"]["public_source"], "path")
        self.assertFalse(self.routes["status"]["public"])
        self.assertEqual(lines_of(self.report, "CONTRACT_ROUTE_AUTH_REQUIRED"), [35, 45])  # /api/status and /api/orders
        self.assertEqual(lines_of(self.report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"), [45])  # convention keeps the informational rule silent
        self.assertEqual(self.routes["health"]["bucket"], "exposure_undetermined")
        self.assertEqual(self.routes["prices"]["bucket"], "public_declared")
        partition = self.report["contracts"]["partition"]
        self.assertEqual(sum(partition.values()), len(self.routes))
        self.assertEqual((partition["public_declared"], partition["exposure_undetermined"], partition["unauthenticated_not_public"]), (1, 2, 1))

    def test_one_question_per_route(self) -> None:
        questions = {}
        for f in self.report["findings"]:
            if f["rule"] in {"SEC_TENANT_NOT_BOUND_TO_PRINCIPAL", "SEC_RLS_CONTEXT_FROM_REQUEST", "SEC_PRIVILEGE_CHANGE_UNVERIFIED", "SEC_RESOURCE_AUTHZ_UNVERIFIED"}:
                questions.setdefault(f["function"], []).append(f["rule"])
        self.assertEqual(questions["reroute"], ["SEC_TENANT_NOT_BOUND_TO_PRINCIPAL"])
        self.assertEqual(questions["set_role"], ["SEC_PRIVILEGE_CHANGE_UNVERIFIED"])
        self.assertEqual(questions["shipment"], ["SEC_RESOURCE_AUTHZ_UNVERIFIED"])
        self.assertEqual(self.routes["reroute"]["resource_params"], ["shipment_id"])  # the id stays visible in the contract
        self.assertEqual(self.routes["reroute"]["tenant_params"], ["depot_id"])
