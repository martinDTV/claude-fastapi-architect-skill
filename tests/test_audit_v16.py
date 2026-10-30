"""v1.6 engine and rules: symbol resolution (relative imports, re-exports, attributes, returns,
callbacks, interfaces, module-level instances), honest coverage and uncertainty reporting,
the reliability and security rules added in v1.6, extensions of existing rules through
instance attributes and call paths, analyser robustness limits, and schema 1.4 contracts.

The ground-truth corpus under tests/fixtures/truth16 is scored with scripts/audit_precision.py:
every marker must be matched and no unexpected finding of an evaluated rule may appear."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS, ProjectCase, audit, audit_fixture, by_rule, lines_of

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
from fastapi_audit.discovery import iter_python_files  # noqa: E402
from fastapi_audit.facts import collect_facts, load_config  # noqa: E402


def _load_precision():
    spec = importlib.util.spec_from_file_location("audit_precision_under_test", REPO_ROOT / "scripts" / "audit_precision.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


precision = _load_precision()
TRUTH = FIXTURES / "truth16"


def graph_for(root: Path):
    config, src = load_config(str(root))
    files, _ = iter_python_files(str(root))
    facts = collect_facts(str(root), files, config=config, config_source=src)
    return facts, facts.callgraph


# --------------------------------------------------------------------------- #
# ground truth
# --------------------------------------------------------------------------- #


class TruthCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.truth = precision.load_truth(str(TRUTH))
        cls.engine_run = precision.collect_run(str(TRUTH))
        cls.score = precision.score(cls.truth, cls.engine_run)

    def test_markers_cover_the_rules_under_evaluation(self) -> None:
        self.assertGreaterEqual(len(self.truth["expected"]), 16)
        self.assertGreaterEqual(len(self.truth["absent"]), 17)
        self.assertEqual(len(self.truth["edges"]), 7)
        self.assertEqual(len(self.truth["unresolved"]), 4)
        self.assertIn("SEC_TENANT_NOT_BOUND_TO_PRINCIPAL", self.score["rules_evaluated"])
        self.assertIn("ASYNC_TASK_REFERENCE_DROPPED", self.score["rules_evaluated"])

    def test_every_expected_finding_is_reported_and_nothing_else(self) -> None:
        self.assertEqual(self.score["false_negatives"], [])
        self.assertEqual(self.score["false_positives"], [])
        self.assertEqual(self.score["tp"], len(self.truth["expected"]))
        self.assertEqual(self.score["tn"], len(self.truth["absent"]))
        self.assertEqual((self.score["precision"], self.score["recall"], self.score["f1"]), (1.0, 1.0, 1.0))

    def test_expected_edges_resolve_and_uncertain_sites_stay_unresolved(self) -> None:
        self.assertEqual(self.score["edges"]["missing"], [])
        self.assertEqual(self.score["unresolved"]["wrong"], [])
        self.assertEqual(self.score["project_call_coverage"], 0.929)  # 2 ambiguous sites are counted as resolvable and unresolved
        self.assertGreaterEqual(self.score["classified_ratio"], 0.97)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


class ResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.facts, cls.graph = graph_for(TRUTH)
        cls.report = audit_fixture("truth16")
        cls.grouped = by_rule(cls.report)
        cls.inter = cls.report["interprocedural"]

    def site(self, rel: str, line: int):
        hits = [(loc, v) for loc, v in self.graph.site_targets.items() if loc[0] == rel and loc[1] == line]
        self.assertTrue(hits, f"no call site at {rel}:{line}")
        return hits

    def test_relative_imports_resolve_to_absolute_names(self) -> None:
        # router.py: `from . import service` + `service.summary(...)` three levels below the package
        statuses = {v for _loc, v in self.site("app/modules/accounts/router.py", 21)}
        self.assertIn(("project", "app.modules.accounts.service.summary"), statuses)
        self.assertEqual(self.facts.reexports["app.modules.accounts.router.service"], "app.modules.accounts.service")

    def test_reexports_canonicalise_through_facades(self) -> None:
        self.assertEqual(self.facts.canonical("app.core.AsyncSession"), "sqlalchemy.ext.asyncio.AsyncSession")
        self.assertEqual(self.facts.canonical("app.core.get_context"), "app.core.security.get_context")
        self.assertEqual(self.facts.canonical("app.modules.accounts.AccountRepository.get"), "app.modules.accounts.repository.AccountRepository.get")
        self.assertEqual(self.graph.canonical_function("app.core.get_current_user"), "app.core.security.get_current_user")

    def test_instance_attributes_typed_in_init_carry_kinds_and_edges(self) -> None:
        kinds = self.graph.attribute_kinds("app/modules/accounts/repository.py", "AccountRepository")
        self.assertEqual(kinds["_session"], ("AsyncSession", "verified"))  # through the facade re-export
        edges = self.inter["edges"]
        self.assertGreaterEqual(edges["attribute"], 1)  # self._primary.get() -> AccountRepository.get
        self.assertEqual(self.graph.edge_info("app/modules/accounts/service.py", 52, 21)[0], "attribute")
        cls = self.graph.class_record_for("app/modules/accounts/service.py", "AccountService")
        self.assertEqual(cls.attrs["_repo"].type, "app.modules.accounts.ports.AccountPort")
        self.assertEqual(cls.attrs["_client"].kind, "AsyncClient")

    def test_return_annotation_edges_and_static_methods(self) -> None:
        self.assertIn(("project", "app.modules.accounts.repository.AccountRepository.get"), {v for _l, v in self.site("app/modules/accounts/factories.py", 15)})
        self.assertIn(("project", "app.modules.accounts.factories.Tools.helper"), {v for _l, v in self.site("app/modules/accounts/factories.py", 29)})
        self.assertIn(("project", "app.modules.accounts.factories.Tools.build"), {v for _l, v in self.site("app/modules/accounts/factories.py", 33)})
        self.assertGreaterEqual(self.inter["edges"]["return"], 1)

    def test_callbacks_resolve_only_when_every_caller_agrees(self) -> None:
        unique = {v for _l, v in self.site("app/modules/accounts/callbacks.py", 16)}
        self.assertIn(("project", "app.modules.accounts.callbacks.slow"), unique)
        loc = [l for l, _v in self.site("app/modules/accounts/callbacks.py", 20)][0]
        self.assertEqual(self.graph.site_targets[loc], ("ambiguous", None))
        self.assertEqual(self.graph.site_candidates[loc], ["app.modules.accounts.callbacks.fast", "app.modules.accounts.callbacks.slow"])
        self.assertEqual(self.graph.edge_info("app/modules/accounts/callbacks.py", 16, 11)[0], "callback")
        self.assertEqual(self.inter["edges"]["callback"], 1)

    def test_interfaces_single_implementation_versus_ambiguous(self) -> None:
        self.assertEqual(self.graph.implementations("app.modules.accounts.ports.NotifierPort"), ["app.modules.notify.email.EmailNotifier"])
        self.assertEqual(self.graph.implementations("app.modules.accounts.ports.AccountPort"), ["app.modules.accounts.repository.AccountRepository", "app.modules.accounts.repository.CachedAccountRepository", "app.modules.accounts.repository.MemoryAccountRepository"])
        send = self.site("app/modules/accounts/service.py", 46)
        self.assertIn(("project", "app.modules.notify.email.EmailNotifier.send"), {v for _l, v in send})
        self.assertEqual(self.graph.edge_info("app/modules/accounts/service.py", 46, 8)[1], "inferred")
        through = [l for l, _v in self.site("app/modules/accounts/service.py", 43)][0]
        self.assertEqual(self.graph.site_targets[through][0], "ambiguous")
        self.assertEqual(self.inter["uncertainty"]["interfaces"], 2)
        self.assertEqual(self.inter["uncertainty"]["ambiguous_sites"], 2)

    def test_builtins_objects_and_external_receivers_are_classified(self) -> None:
        res = self.inter["resolution"]
        self.assertGreaterEqual(res["builtin"], 2)
        self.assertGreaterEqual(res["external"], 10)
        self.assertEqual(sum(res.values()), self.inter["call_sites"])
        self.assertEqual(sum(self.inter["edges"].values()), res["project"])
        self.assertIn(("builtin", None), {v for _l, v in self.site("app/modules/accounts/factories.py", 37)})
        self.assertIn(("builtin", None), {v for _l, v in self.site("app/modules/accounts/repository.py", 41)})

    def test_inferred_paths_lower_confidence_and_say_so(self) -> None:
        items = {f["line"]: f for f in self.grouped["ASYNC_BLOCKING_CALL_INDIRECT"] if f["file"] == "app/modules/accounts/service.py"}
        self.assertEqual(items[46]["evidence"], "inferred")
        self.assertEqual(items[46]["confidence"], "medium")
        self.assertIn("interface, a callback or a caller-typed value", items[46]["message"])
        self.assertEqual(items[46]["call_path"][-1], "requests.post")
        via_callback = [f for f in self.grouped["ASYNC_BLOCKING_CALL_INDIRECT"] if f["file"] == "app/modules/accounts/callbacks.py"]
        self.assertEqual([(f["line"], f["evidence"]) for f in via_callback], [(24, "inferred")])


class ShadowingAndModuleInstanceTests(ProjectCase):
    def test_a_parameter_shadows_a_module_level_definition(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/t.py", '''
            import httpx

            def client() -> httpx.Client:
                return httpx.Client()

            def use(client: httpx.AsyncClient) -> None:
                client.get("/x")          # the parameter, not the module-level function
        ''')
        _facts, graph = graph_for(self.root)
        self.assertEqual(graph.site_target("app/t.py", 7, 4)[0], "external")
        self.assertEqual(graph.stats.not_found, 0)

    def test_module_level_instance_of_a_project_class_resolves_from_other_modules(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/events.py", '''
            class Dispatcher:
                async def publish(self, session, event) -> None:
                    session.add(event)

            dispatcher = Dispatcher()
        ''')
        self.write("app/svc.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.events import dispatcher

            async def act(session: AsyncSession) -> None:
                session.add(object())
                await session.commit()
                await dispatcher.publish(session, "done")   # project-owned, in-process: not a dual write
        ''')
        _facts, graph = graph_for(self.root)
        self.assertEqual(graph.site_target("app/svc.py", 7, 10), ("project", "app.events.Dispatcher.publish"))
        self.assertEqual(self.rules(), [])


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #


class ReliabilityRuleTests(ProjectCase):
    def test_requests_session_without_timeout_is_reported_but_httpx_clients_are_not(self) -> None:
        self.write("app/h.py", '''
            import httpx
            import requests

            def a() -> None:
                s = requests.Session()
                s.get("https://x.invalid")

            def b(client: httpx.Client) -> None:
                client.get("https://x.invalid")       # httpx has a 5 s default timeout

            def c() -> None:
                with httpx.Client() as client:
                    client.get("https://x.invalid")
        ''')
        self.assertEqual(lines_of(self.run_audit(), "REL_HTTP_CALL_WITHOUT_TIMEOUT"), [6])

    def test_unbounded_retry_decorator_and_loop_confidence(self) -> None:
        report = audit_fixture("truth16")
        items = {f["line"]: f for f in by_rule(report)["RETRY_UNBOUNDED"]}
        self.assertEqual(sorted(items), [31, 42])
        self.assertEqual((items[31]["confidence"], items[42]["confidence"]), ("high", "medium"))
        self.assertIn("retries forever", items[31]["message"])

    def test_dual_write_skips_project_owned_queues_that_stay_in_the_database(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/jobs.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession

            async def enqueue(session: AsyncSession, name: str) -> None:
                session.add(object())            # a jobs table in the same database
        ''')
        self.write("app/svc.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            from app import jobs

            kafka = None

            async def ok(session: AsyncSession) -> None:
                session.add(object())
                await jobs.enqueue(session, "x")
                await session.commit()

            async def dual(session: AsyncSession) -> None:
                session.add(object())
                await session.commit()
                await kafka.send_and_wait("topic", b"x")
        ''')
        self.assertEqual(lines_of(self.run_audit(), "REL_DUAL_WRITE_WITHOUT_OUTBOX"), [14])

    def test_task_reference_rule_is_silent_in_tests_and_for_kept_references(self) -> None:
        report = audit_fixture("truth16")
        self.assertEqual([(f["file"], f["line"]) for f in by_rule(report)["ASYNC_TASK_REFERENCE_DROPPED"]], [("app/modules/accounts/tasks.py", 18)])

    def test_v16_reliability_checks_run_without_the_call_graph(self) -> None:
        self.write("app/h.py", '''
            import asyncio
            import requests

            async def a() -> None:
                asyncio.create_task(asyncio.sleep(0))
                requests.get("https://x.invalid")
        ''')
        rules = self.rules(options=audit.AuditOptions(interprocedural=False))
        self.assertIn("ASYNC_TASK_REFERENCE_DROPPED", rules)
        self.assertIn("REL_HTTP_CALL_WITHOUT_TIMEOUT", rules)


class CalibrationRegressionTests(unittest.TestCase):
    """Shapes that were false positives on real repositories; each has a truth-absent marker."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("truth16")

    def test_loop_retries_count_only_writes_inside_the_retried_block(self) -> None:
        lines = lines_of(self.report, "RETRY_NON_IDEMPOTENT_WRITE", "app/modules/accounts/batches.py")
        self.assertEqual(lines, [68])  # retried_write; not the give-up write (20) nor the two-pass purge (46)
        self.assertEqual(lines_of(audit_fixture("interproc"), "RETRY_NON_IDEMPOTENT_WRITE", "src/app/retry.py"), [11, 25])

    def test_commit_in_loop_ignores_token_refresh_retries_and_explicit_batches(self) -> None:
        self.assertEqual(lines_of(self.report, "TX_COMMIT_IN_LOOP", "app/modules/accounts/batches.py"), [])
        self.assertEqual(lines_of(self.report, "TX_COMMIT_IN_LOOP", "app/modules/accounts/repository.py"), [27])

    def test_permission_factory_dependencies_count_as_policy_and_auth_signals(self) -> None:
        self.assertEqual(lines_of(self.report, "SEC_PRIVILEGE_CHANGE_UNVERIFIED", "app/modules/accounts/batches.py"), [])
        self.assertEqual(lines_of(self.report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL", "app/modules/accounts/batches.py"), [])
        facts, _graph = graph_for(TRUTH)
        self.assertEqual(facts.dependency_aliases["app.modules.accounts.batches.GrantContext"], "app.modules.accounts.batches.require")
        self.assertIn("PERMISSIONS['grant']", facts.dependency_alias_labels["app.modules.accounts.batches.GrantContext"])


class SecurityRuleTests(ProjectCase):
    def test_authentication_through_a_composed_dependency_is_recognised(self) -> None:
        report = audit_fixture("truth16")
        self.assertEqual(lines_of(report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL", "app/modules/accounts/router.py"), [])

    def test_tenant_binding_finding_is_a_question_with_a_cross_tenant_test(self) -> None:
        report = audit_fixture("truth16")
        items = by_rule(report)["SEC_TENANT_NOT_BOUND_TO_PRINCIPAL"]
        self.assertEqual([(f["file"], f["line"]) for f in items], [("app/modules/accounts/router.py", 25)])
        f = items[0]
        self.assertEqual((f["confidence"], f["evidence"], f["verification"]), ("low", "unknown", "not_verifiable_statically"))
        self.assertIn("no visible step binds the two", f["message"])
        self.assertIn("tenant B", f["recommended_test"])

    def test_privilege_change_without_admin_signal_versus_guarded(self) -> None:
        report = audit_fixture("truth16")
        items = by_rule(report)["SEC_PRIVILEGE_CHANGE_UNVERIFIED"]
        self.assertEqual([(f["file"], f["line"], f["evidence"]) for f in items], [("app/modules/accounts/router.py", 40, "unknown")])
        self.assertIn("user.role = ...", items[0]["message"])

    def test_tenant_compared_with_the_principal_counts_as_bound(self) -> None:
        self.write("app/r.py", '''
            from uuid import UUID
            from fastapi import APIRouter, Depends

            router = APIRouter()

            async def current_user():
                return {"tenants": set()}

            @router.get("/t/{tenant_id}")
            async def read(tenant_id: UUID, user=Depends(current_user)) -> dict:
                if tenant_id not in user["tenants"]:
                    raise PermissionError
                return {"ok": tenant_id}
        ''')
        self.assertEqual(self.findings("SEC_TENANT_NOT_BOUND_TO_PRINCIPAL"), [])


class ExtendedRuleTests(ProjectCase):
    def test_shared_session_through_methods_of_the_same_object(self) -> None:
        report = audit_fixture("truth16")
        items = [f for f in by_rule(report)["SA_SESSION_SHARED_CONCURRENTLY"] if f["file"] == "app/modules/accounts/service.py"]
        self.assertEqual([(f["line"], f["confidence"], f["evidence"]) for f in items], [(29, "high", "verified")])
        self.assertEqual(items[0]["call_path"], ["AccountService.transfer", "AccountService._debit", "AccountService._credit", "self._session"])

    def test_async_session_rules_reach_instance_attributes(self) -> None:
        report = audit_fixture("truth16")
        self.assertEqual(lines_of(report, "SA_ASYNC_SESSION_NOT_AWAITED", "app/modules/accounts/repository.py"), [17])
        self.assertEqual(lines_of(report, "TX_COMMIT_IN_LOOP", "app/modules/accounts/repository.py"), [27])
        tx = [f for f in by_rule(report)["TX_EXTERNAL_CALL_IN_TRANSACTION"] if f["file"] == "app/modules/accounts/service.py"]
        self.assertEqual([(f["line"], f["confidence"]) for f in tx], [(40, "high")])

    def test_external_call_in_transaction_through_a_project_call_path(self) -> None:
        self.write("app/__init__.py", "")
        self.write("app/notify.py", '''
            import httpx

            async def ping(url: str) -> None:
                async with httpx.AsyncClient() as client:
                    await client.get(url)
        ''')
        self.write("app/svc.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.notify import ping

            async def act(session: AsyncSession) -> None:
                async with session.begin():
                    session.add(object())
                    await ping("https://x.invalid")
        ''')
        items = self.findings("TX_EXTERNAL_CALL_IN_TRANSACTION")
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["evidence"]) for f in items], [("app/svc.py", 7, "medium", "verified")])
        self.assertEqual(items[0]["call_path"], ["act", "app.notify.ping", "client.get"])

    def test_commit_without_rollback_ignores_domain_exception_handlers(self) -> None:
        self.write("app/svc.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession

            class NeedsReauth(Exception):
                pass

            async def a(session: AsyncSession) -> None:
                try:
                    await session.commit()
                except NeedsReauth:
                    return None                 # cannot be the commit failing

            async def b(session: AsyncSession) -> None:
                try:
                    await session.commit()
                except Exception:
                    return None                 # swallows a failed commit
        ''')
        self.assertEqual(lines_of(self.run_audit(), "TX_COMMIT_WITHOUT_ROLLBACK"), [15])


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #


class RobustnessTests(ProjectCase):
    def test_deep_expression_in_a_rule_path_yields_partial_analysis_not_a_crash(self) -> None:
        chain = "+".join(["1"] * 3000)
        self.write("app/deep.py", f'''
            import asyncio

            async def retry(client) -> None:
                while True:
                    try:
                        await client.get("x")
                        x = {chain}
                        break
                    except Exception:
                        await asyncio.sleep(1)
        ''')
        self.write("app/route.py", f'''
            from fastapi import APIRouter, Depends

            router = APIRouter()

            async def current_user():
                return {{}}

            @router.post("/role")
            async def set_role(user=Depends(current_user)) -> dict:
                if {chain} > 0:
                    user.role = "admin"
                return {{}}
        ''')
        self.write("app/ok.py", "import time\nasync def h():\n    time.sleep(1)\n")
        report = self.run_audit()
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL", "app/ok.py"), [3])  # the other file is unaffected
        partial = {(f["file"], "summar" in f["message"]) for f in report["findings"] if f["rule"] == "ANALYSIS_PARTIAL"}
        self.assertIn(("app/deep.py", True), partial)      # summaries skipped the file (ast.dump recursion in the loop scan)
        self.assertIn(("app/route.py", False), partial)    # the security module hit the limit on the deep `if` test
        self.assertEqual(report["interprocedural"]["files_skipped"], 1)  # route.py summarised fine; only its security pass stopped
        self.assertTrue(report["summary"]["partial_analysis"])

    def test_expression_beyond_parser_limits_is_skipped_and_reported(self) -> None:
        self.write("app/huge.py", "x = " + "+".join(["1"] * 100000) + "\n")
        self.write("app/parens.py", "x = " + "(" * 300 + "1" + ")" * 300 + "\n")
        report = self.run_audit()
        rules = {f["file"]: f["rule"] for f in report["findings"]}
        self.assertEqual(rules.get("app/parens.py"), "PY_SYNTAX")
        # the 100k-term chain exhausts the parser on CPython 3.11+ (RecursionError -> FILE_SKIPPED);
        # older parsers accept it, in which case the file is analysed normally. Either way: no crash.
        self.assertIn(rules.get("app/huge.py"), {"FILE_SKIPPED", "ANALYSIS_PARTIAL", None})
        if rules.get("app/huge.py") == "FILE_SKIPPED":
            self.assertIn("parser limits", [f for f in report["findings"] if f["file"] == "app/huge.py"][0]["message"])
        self.assertTrue(report["summary"]["partial_analysis"])  # PY_SYNTAX alone already makes it partial
        self.assertTrue(report["interprocedural"]["enabled"])

    def test_time_budget_truncates_the_graph_and_is_reported_deterministically(self) -> None:
        for i in range(60):
            self.write(f"app/m{i}.py", "import time\n\ndef f(x):\n    return g(x)\n\ndef g(x):\n    time.sleep(1)\n    return x\n")
        options = audit.AuditOptions(time_budget_s=0.0005)
        first = self.run_audit(options=options)
        second = self.run_audit(options=options)
        for report in (first, second):
            self.assertTrue(report["interprocedural"]["truncated"])
            self.assertIn("time budget", report["interprocedural"]["truncated_reason"])
            self.assertEqual(report["interprocedural"]["limits"]["time_budget_s"], 0.0005)
            self.assertTrue(report["summary"]["partial_analysis"])
        # a wall-clock budget cannot be deterministic in what it cuts; what is deterministic is that
        # the report says it was cut. Without truncation the same project is byte-identical:
        full_a = audit.to_json(self.run_audit())
        full_b = audit.to_json(self.run_audit())
        self.assertEqual(full_a, full_b)
        self.assertFalse(json.loads(full_a)["interprocedural"]["truncated"])

    def test_large_synthetic_repository_completes_within_budget(self) -> None:
        for i in range(150):
            body = "\n".join(f"async def f{i}_{j}(session: AsyncSession, repo: Repo):\n    await session.execute('q')\n    return await repo.get({j})" for j in range(12))
            self.write(f"app/pkg{i % 10}/mod{i}.py", f"from sqlalchemy.ext.asyncio import AsyncSession\nfrom app.repo import Repo\n\n{body}\n")
        self.write("app/repo.py", "class Repo:\n    async def get(self, i):\n        return i\n")
        start = time.monotonic()
        report = self.run_audit()
        self.assertLess(time.monotonic() - start, 30.0)
        inter = report["interprocedural"]
        self.assertFalse(inter["truncated"])
        self.assertEqual(inter["functions"], 150 * 12 + 1)
        self.assertEqual(inter["resolution"]["project"], 150 * 12)
        self.assertEqual(inter["coverage"]["project_call_coverage"], 1.0)

    def test_corpus_report_is_deterministic(self) -> None:
        self.assertEqual(audit.to_json(audit_fixture("truth16")), audit.to_json(audit_fixture("truth16")))


# --------------------------------------------------------------------------- #
# contracts
# --------------------------------------------------------------------------- #


class SchemaContractTests(ProjectCase):
    def test_schema_1_4_is_additive(self) -> None:
        report = audit_fixture("truth16")
        self.assertGreaterEqual(tuple(int(x) for x in report["schema_version"].split(".")), (1, 4))
        self.assertGreaterEqual(tuple(int(x) for x in report["tool"]["version"].split(".")), (1, 6, 0))
        inter = report["interprocedural"]
        for key in ("enabled", "max_depth", "functions", "classes", "call_sites", "resolution", "dependency_params", "inferred_params", "propagation_rounds", "truncated", "truncated_reason"):
            self.assertIn(key, inter)  # 1.3
        for key in ("edges", "coverage", "uncertainty", "limits", "attribute_kinds", "files_skipped"):
            self.assertIn(key, inter)  # 1.4
        self.assertEqual(set(inter["resolution"]), {"project", "external", "dynamic", "not_found", "constructors", "builtin", "object", "ambiguous"})
        self.assertEqual(set(inter["limits"]), {"max_depth", "time_budget_s", "max_base_walk", "max_implementations", "propagation_rounds_max"})
        self.assertIn("time_budget_s", report["config"])
        for finding in report["findings"]:
            self.assertIn(finding["evidence"], {"verified", "inferred", "unknown"})
            self.assertIn("call_path", finding)
        for rule in ("ASYNC_TASK_REFERENCE_DROPPED", "RETRY_UNBOUNDED", "REL_HTTP_CALL_WITHOUT_TIMEOUT", "REL_DUAL_WRITE_WITHOUT_OUTBOX", "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL", "SEC_PRIVILEGE_CHANGE_UNVERIFIED", "ANALYSIS_PARTIAL"):
            self.assertIn(rule, audit.RULES)
        self.assertGreaterEqual(len(audit.RULES), 48)

    def test_cli_time_budget_and_no_interprocedural_options(self) -> None:
        self.write("app/a.py", "import time\nasync def h():\n    time.sleep(1)\n")
        proc = self.cli(".", "--format", "json", "--time-budget", "2.5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["config"]["time_budget_s"], 2.5)
        self.assertEqual(report["interprocedural"]["limits"]["time_budget_s"], 2.5)
        off = json.loads(self.cli(".", "--format", "json", "--no-interprocedural").stdout)
        self.assertFalse(off["interprocedural"]["enabled"])
        self.assertEqual(off["config"]["interprocedural"], False)

    def test_fingerprints_are_stable_across_versions_for_unchanged_findings(self) -> None:
        # a 1.3 baseline with only the old keys still compares (additive schema)
        report = audit_fixture("interproc")
        legacy = {"schema_version": "1.3", "findings": [{k: v for k, v in f.items() if k not in {"evidence", "call_path"}} for f in report["findings"] if f["rule"] != "ASYNC_TASK_REFERENCE_DROPPED"]}
        compared = audit.audit_project(str(FIXTURES / "interproc"), options=audit.AuditOptions(baseline=legacy, baseline_name="v15.json"))["comparison"]
        self.assertEqual(compared["counts"]["new"], 1)  # the v1.6 task-reference finding
        self.assertEqual(compared["counts"]["fixed"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
