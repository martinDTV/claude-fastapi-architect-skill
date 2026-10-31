"""v2.0 security hardening: a dependency named like a policy is not a policy.

Until this change the route contracts recorded the authorization kind ``role`` for any ``Depends()`` whose name
matched ``admin|owner|role|perm|require|guard|...`` (``ROLE_RE``), the privilege-change rule went silent on the same
names (``ADMIN_RE``) and the tenant-binding rule accepted ``member|authorize|policy|...`` names as a binding. A
dependency called ``require_admin`` that returns the user unchanged therefore made a route "authorization visible"
and silenced three questions. Now a role/permission kind needs structural evidence in the dependency's own code,
anywhere in its chain, its ``__call__`` or its closures, followed one project call deep: a condition or predicate
over a role/scope/permission attribute or predicate, or ``Security(scopes=...)``. Names without evidence are
recorded as ``authorization_named_only`` and leave the route's authorization unknown, so the declared-contract rule
(``mutation_authorization_required``) and the resource-authorization question apply. The fixture
``tests/fixtures/regressions_v20/named_authz`` holds the seven negative controls the review asked for and six
positive controls that must keep their kind.
"""

from __future__ import annotations

import unittest

from _audit_support import FIXTURES, audit


class NamedAuthorizationTests(unittest.TestCase):
    NEGATIVE = {
        "patch_nothing": "require_nothing",  # 1. no check at all
        "patch_none": "require_admin_none",  # 2. returns None, checks nothing
        "patch_any": "require_any_token",  # 3. accepts any token
        "patch_authn": "require_authenticated",  # 4. authentication, not authorization
        "patch_editor": "require_editor",  # 5. hands the principal on without validating permissions
        "patch_owner": "require_owner",  # 6. reassuring name, never looks at the principal
        "patch_nested": "get_ctx",  # 7. neutral name over a nested name-only dependency
        "patch_external": "current_superuser",  # not in the project: nothing can be verified
    }
    POSITIVE = ("patch_admin", "patch_scope", "patch_checker", "patch_nested_ok", "patch_guard", "patch_gate")

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit.audit_project(str(FIXTURES / "regressions_v20" / "named_authz"))
        cls.findings = [f for f in cls.report["findings"] if not f.get("suppressed")]
        cls.routes = {c["function"]: c for c in cls.report["contracts"]["routes"]}

    def _rules_at(self, line: int):
        return sorted(f["rule"] for f in self.findings if f["file"] == "named/api.py" and f["line"] == line)

    def test_names_alone_never_yield_an_authorization_kind(self) -> None:
        for function, dependency in self.NEGATIVE.items():
            with self.subTest(function=function):
                c = self.routes[function]
                self.assertEqual(c["authentication"], "present", c)
                self.assertEqual(c["authorization"], [], c)
                self.assertEqual(c["evidence"], "unknown")
                self.assertEqual(c["bucket"], "authenticated_authorization_unknown")
                self.assertTrue(any(dependency in n and "named like a policy" in n for n in c["authorization_named_only"]), c["authorization_named_only"])

    def test_named_only_evidence_says_why(self) -> None:
        self.assertIn("no check on the principal is visible in require_nothing -> get_current_user", self.routes["patch_nothing"]["authorization_named_only"][0])
        self.assertIn("its implementation is not in the project", self.routes["patch_external"]["authorization_named_only"][0])
        self.assertIn("get_ctx -> require_admin_none", self.routes["patch_nested"]["authorization_named_only"][0])

    def test_visible_decisions_keep_their_kind_and_carry_the_line(self) -> None:
        for function in self.POSITIVE:
            with self.subTest(function=function):
                c = self.routes[function]
                self.assertIn("role", c["authorization"], c)
                self.assertEqual(c["evidence"], "verified")
                self.assertEqual(c["authorization_named_only"], [])
                self.assertTrue(any("line " in e and (" on role" in e or " on scope" in e or "denial on" in e) for e in c["authorization_evidence"]), c["authorization_evidence"])
                self.assertEqual(self._rules_at(c["line"]), [])

    def test_class_based_factory_based_nested_and_delegated_policies_are_followed(self) -> None:
        self.assertIn("RoleChecker", self.routes["patch_checker"]["authorization_evidence"][-1])  # __call__ of a class-based dependency
        self.assertIn("dependency:", self.routes["patch_scope"]["authorization_evidence"][-1])  # the closure returned by require_scope("items:write")
        self.assertIn("require_admin:", self.routes["patch_nested_ok"]["authorization_evidence"][-1])  # one level below get_admin_ctx
        self.assertIn("guard: line ", self.routes["patch_guard"]["authorization_evidence"][-1])  # its own `raise HTTPException(403)`
        self.assertIn("denial on HTTPException(403)", self.routes["patch_guard"]["authorization_evidence"][-1])
        self.assertIn("gate -> gate_outcome: line ", self.routes["patch_gate"]["authorization_evidence"][-1])  # the decision one call away

    def test_router_level_dependencies_follow_the_same_rule(self) -> None:
        ops, named = self.routes["ops_patch"], self.routes["named_patch"]
        self.assertEqual(sorted(ops["authorization"]), ["role", "router_dependencies"])
        self.assertEqual(ops["evidence"], "verified")
        self.assertEqual(named["authentication"], "present")  # the router dependency still authenticates
        self.assertEqual(named["authorization"], [])
        self.assertTrue(named["authorization_named_only"] and named["authorization_named_only"][0].startswith("router: require_nothing"))

    def test_the_declared_contract_catches_every_named_only_mutation_and_no_verified_one(self) -> None:
        flagged = sorted(f["line"] for f in self.findings if f["rule"] == "CONTRACT_MUTATION_AUTHZ_REQUIRED")
        expected = sorted(self.routes[name]["line"] for name in list(self.NEGATIVE) + ["named_patch"])
        self.assertEqual(flagged, expected)
        for f in self.findings:
            if f["rule"] == "CONTRACT_MUTATION_AUTHZ_REQUIRED":
                self.assertEqual(f["confidence"], "high")
        self.assertEqual([f for f in self.findings if f["rule"] == "CONTRACT_ROUTE_AUTH_REQUIRED"], [])  # every route is authenticated

    def test_the_resource_question_is_asked_where_a_principal_exists(self) -> None:
        asked = sorted(f["line"] for f in self.findings if f["rule"] == "SEC_RESOURCE_AUTHZ_UNVERIFIED")
        with_principal = [name for name in list(self.NEGATIVE) + ["named_patch"] if self.routes[name]["principal_kind"] != "none"]
        self.assertEqual(asked, sorted(self.routes[name]["line"] for name in with_principal))
        # `_: None = Depends(require_admin_none)`: a shared-credential check with no principal to bind (v1.8 rule), still a
        # declared-contract violation, never "authorization visible"
        self.assertEqual(self.routes["patch_none"]["principal_kind"], "none")
        self.assertEqual(self._rules_at(self.routes["patch_none"]["line"]), ["CONTRACT_MUTATION_AUTHZ_REQUIRED"])
        for f in self.findings:
            if f["rule"] == "SEC_RESOURCE_AUTHZ_UNVERIFIED":
                self.assertEqual((f["confidence"], f.get("evidence_state")), ("low", "insufficient"))  # a question, never a confirmed defect

    def test_a_bare_none_annotation_is_not_optional_authentication(self) -> None:
        self.assertEqual(self.routes["patch_none"]["authentication"], "present")
        self.assertEqual(self.routes["patch_owner"]["authentication"], "present")
        self.assertEqual(self.report["contracts"]["summary"]["optional_authentication"], 0)

    def test_summary_counts_the_uncertainty(self) -> None:
        summary = self.report["contracts"]["summary"]
        self.assertEqual(summary["authorization_unknown"], len(self.NEGATIVE) + 1)
        self.assertEqual(summary["authorization_visible"], len(self.POSITIVE) + 1)
        self.assertEqual(summary["unauthenticated_not_public"], 0)
        self.assertNotIn("named_only", self.report["contracts"]["authorization_kinds"])  # never aggregated as a kind


class NamedPolicyInExistingCorporaTests(unittest.TestCase):
    """The ground-truth corpora keep their classification: their policy dependencies show their decision."""

    def test_truth17_dev_contracts_keep_role_scopes_and_membership(self) -> None:
        report = audit.audit_project(str(FIXTURES / "truth17" / "dev"))
        routes = {c["function"]: c for c in report["contracts"]["routes"]}
        self.assertIn("role", routes["admin_stats"]["authorization"])
        self.assertIn("role", routes["delete_account"]["authorization"])  # require("accounts:delete"): the closure compares scopes
        self.assertIn("role", routes["queues"]["authorization"])  # router-level RoleChecker("admin")
        self.assertIn("membership", routes["invoices"]["authorization"])
        self.assertEqual(routes["close_account"]["authorization"], [])

    def test_privilege_change_rule_still_distinguishes_a_real_admin_dependency(self) -> None:
        report = audit.audit_project(str(FIXTURES / "truth16"))
        lines = sorted(f["line"] for f in report["findings"] if f["rule"] == "SEC_PRIVILEGE_CHANGE_UNVERIFIED")
        self.assertEqual(lines, [40])  # set_role; set_role_guarded (require_admin compares the role) and grant_access stay silent

    def test_privilege_change_and_tenant_binding_keep_their_question_behind_a_policy_like_name(self) -> None:
        fixture = FIXTURES / "regressions_v20" / "named_authz_privilege"
        source = (fixture / "priv" / "api.py").read_text(encoding="utf-8").splitlines()
        line_of = lambda name: next(i + 1 for i, l in enumerate(source) if l.startswith(f"async def {name}("))  # noqa: E731
        report = audit.audit_project(str(fixture))
        privilege = [f for f in report["findings"] if f["rule"] == "SEC_PRIVILEGE_CHANGE_UNVERIFIED"]
        self.assertEqual([f["line"] for f in privilege], [line_of("set_role")])  # set_role_guarded (require_real_admin compares the role) stays silent
        self.assertIn("'require_admin' is named like a policy but no check on the principal is visible", privilege[0]["message"])
        tenant = [f for f in report["findings"] if f["rule"] == "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL"]
        self.assertEqual([f["line"] for f in tenant], [line_of("tenant_items")])  # require_member never receives the tenant
        routes = {c["function"]: c for c in report["contracts"]["routes"]}
        self.assertTrue(routes["tenant_items"]["authorization_named_only"][0].startswith("member: require_member: named like a membership check"))
        self.assertIn("role", routes["set_role_guarded"]["authorization"])


if __name__ == "__main__":
    unittest.main()
