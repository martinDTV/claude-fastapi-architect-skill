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
