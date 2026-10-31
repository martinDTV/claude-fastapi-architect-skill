"""Auditor precision changes shipped with v2.0 (the design engine generates idiomatic router trees).

Protection declared on a router (``APIRouter(dependencies=[...])``) applies, in FastAPI, to every router included
below it. Until v2.0 the auditor only recognised the router that carried the dependencies, so routes mounted
through ``api_router.include_router(child)`` were reported as unauthenticated. The facts pass now records
``X.include_router(Y)`` edges and propagates protection to a fixpoint, in any file order and through several
levels; routers included into an unprotected parent are unchanged (negative control).
"""

from __future__ import annotations

import unittest

from _audit_support import FIXTURES, audit


class NestedRouterProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit.audit_project(str(FIXTURES / "regressions_v20" / "nested_routers"))
        cls.findings = [f for f in cls.report["findings"] if not f.get("suppressed")]
        cls.routes = {c["function"]: c for c in cls.report["contracts"]["routes"]}

    def _rules_for(self, relpath: str):
        return sorted(f["rule"] for f in self.findings if f["file"] == relpath)

    def test_routes_two_levels_below_a_protected_router_are_authenticated(self) -> None:
        self.assertEqual(self._rules_for("src/shop/api/items.py"), [])
        self.assertEqual(self._rules_for("src/shop/api/tenants.py"), [])
        self.assertEqual(self.routes["get_item"]["authentication"], "present")
        self.assertEqual(self.routes["list_things"]["authentication"], "present")

    def test_tenant_parameter_below_a_protected_router_is_not_reported_as_unauthenticated(self) -> None:
        tenant = [f for f in self.findings if f["rule"] == "SEC_TENANT_ID_FROM_REQUEST"]
        self.assertEqual([f["file"] for f in tenant], ["src/shop/api/plain.py"])  # only the negative control

    def test_negative_control_keeps_the_findings(self) -> None:
        self.assertEqual(self._rules_for("src/shop/api/plain.py"), ["SEC_ROUTE_WITHOUT_AUTH_SIGNAL", "SEC_TENANT_ID_FROM_REQUEST"])
        self.assertNotEqual(self.routes["open_item"]["authentication"], "present")

    def test_facts_record_the_propagation(self) -> None:
        self.assertGreaterEqual(self.report["facts"]["protected_routers"], 3)  # api_router, v1_router, items, tenants (under one or more names)
        self.assertFalse(self.report["facts"]["global_auth_signal"])  # nothing app-wide: the precision comes from the edges

    def test_propagation_is_a_fixpoint_independent_of_file_order(self) -> None:
        facts = audit.collect_facts.__self__ if hasattr(audit.collect_facts, "__self__") else None  # noqa: F841 (import guard)
        from fastapi_audit.facts import ProjectFacts, _propagate_protected_routers

        pf = ProjectFacts(root=".")
        pf.protected_routers = {"pkg.api.root"}
        pf.router_inclusions = [(("pkg.api.mid",), ("pkg.api.leaf",)), (("pkg.api.root",), ("pkg.api.mid",))]  # leaf edge first
        _propagate_protected_routers(pf)
        self.assertIn("pkg.api.leaf", pf.protected_routers)
        pf2 = ProjectFacts(root=".")
        pf2.protected_routers = {"pkg.api.other"}
        pf2.router_inclusions = [(("pkg.api.root",), ("pkg.api.mid",))]
        _propagate_protected_routers(pf2)
        self.assertNotIn("pkg.api.mid", pf2.protected_routers)


if __name__ == "__main__":
    unittest.main()


class RouteCompositionTests(unittest.TestCase):
    """v2.0 increment 2: effective paths composed through include_router prefixes.

    Fixture ``route_composition``: ``app.include_router(api_router, prefix="/api")`` with ``api_router = APIRouter(prefix="/v1")``
    including ``items`` (``prefix="/items"``) and ``orders`` (own prefix ``/orders``); ``reports`` mounted twice (``/internal`` and
    ``/partner``); ``dynamic`` mounted with a prefix read from settings. ``public_routes`` declares the full paths of items,
    the partner mount of reports and the dynamic route's assumed full path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit.audit_project(str(FIXTURES / "regressions_v20" / "route_composition"))
        cls.routes = {c["function"]: c for c in cls.report["contracts"]["routes"]}
        cls.findings = [f for f in cls.report["findings"] if not f.get("suppressed")]

    def test_prefixes_compose_through_two_levels_and_full_path_declarations_apply(self) -> None:
        self.assertEqual(self.routes["list_items"]["effective_paths"], ["/api/v1/items"])
        self.assertEqual(self.routes["get_item"]["effective_paths"], ["/api/v1/items/{item_id}"])
        for name in ("list_items", "get_item"):
            self.assertEqual(self.routes[name]["path_resolution"], "resolved")
            self.assertTrue(self.routes[name]["public"])
            self.assertEqual(self.routes[name]["public_source"], "config")
        self.assertEqual([f["function"] for f in self.findings if f["file"] == "src/shop/api/items.py"], [])

    def test_a_routers_own_prefix_is_kept_below_the_mount(self) -> None:
        self.assertEqual(self.routes["get_order"]["effective_paths"], ["/api/v1/orders/{order_id}"])
        self.assertEqual(self.routes["get_order"]["path_resolution"], "resolved")
        self.assertFalse(self.routes["get_order"]["public"])
        self.assertEqual([f["rule"] for f in self.findings if f["function"] == "get_order"], ["SEC_ROUTE_WITHOUT_AUTH_SIGNAL"])

    def test_a_router_mounted_twice_is_ambiguous_and_public_only_if_every_mount_is_declared(self) -> None:
        c = self.routes["get_report"]
        self.assertEqual(c["effective_paths"], ["/internal/reports/{report_id}", "/partner/reports/{report_id}"])
        self.assertEqual(c["path_resolution"], "ambiguous")
        self.assertFalse(c["public"])  # /partner/reports/* is declared, /internal/reports/* is not
        self.assertEqual(len([f for f in self.findings if f["function"] == "get_report"]), 1)  # one finding, not one per mount

    def test_a_non_constant_prefix_is_unresolved_and_nothing_is_invented(self) -> None:
        c = self.routes["dyn"]
        self.assertEqual(c["path_resolution"], "unresolved")
        self.assertEqual(c["effective_paths"], [])
        self.assertFalse(c["public"])  # /dynamic/dyn is declared but the auditor cannot know the prefix
        self.assertEqual([f["rule"] for f in self.findings if f["function"] == "dyn"], ["SEC_ROUTE_WITHOUT_AUTH_SIGNAL"])

    def test_routes_on_the_application_resolve_to_their_own_path(self) -> None:
        self.assertEqual(self.routes["health"]["effective_paths"], ["/health"])
        self.assertEqual(self.routes["health"]["path_resolution"], "resolved")

    def test_router_local_declarations_keep_working(self) -> None:
        # the nested_routers fixture declares nothing; its contracts carry effective paths too
        report = audit.audit_project(str(FIXTURES / "regressions_v20" / "nested_routers"))
        routes = {c["function"]: c for c in report["contracts"]["routes"]}
        self.assertEqual(routes["get_item"]["effective_paths"], ["/api/v1/items/{item_id}"])
        self.assertEqual(routes["list_things"]["effective_paths"], ["/api/v1/tenants/{tenant_id}/things"])
        self.assertEqual(routes["open_item"]["effective_paths"], ["/plain/open/items/{item_id}"])

    def test_fingerprints_do_not_depend_on_effective_paths(self) -> None:
        for f in self.findings:
            self.assertNotIn("/api/v1", f["message"])  # messages keep the handler's own path: fingerprints are stable
