"""Authorization and multi-tenancy review signals (fixtures tenancy_unprotected / tenancy_protected)."""

from __future__ import annotations

import unittest

from _audit_support import ProjectCase, audit_fixture, by_rule, lines_of


class TenancyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.unprotected = audit_fixture("tenancy_unprotected")
        cls.protected = audit_fixture("tenancy_protected")

    def test_tenant_identifier_from_request_without_visible_principal(self) -> None:
        items = by_rule(self.unprotected)["SEC_TENANT_ID_FROM_REQUEST"]
        self.assertEqual([(f["file"], f["line"], f["severity"], f["confidence"], f["verification"]) for f in items],
                         [("app/routes.py", 12, "high", "medium", "not_verifiable_statically")])
        self.assertIn("false positive", items[0]["message"])
        self.assertIn("tenant A", items[0]["recommended_test"])

    def test_route_without_auth_signal_is_informational_and_skips_health(self) -> None:
        items = by_rule(self.unprotected)["SEC_ROUTE_WITHOUT_AUTH_SIGNAL"]
        self.assertEqual([(f["line"], f["severity"], f["confidence"]) for f in items], [(18, "info", "low")])
        self.assertNotIn(23, lines_of(self.unprotected, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"))  # /health

    def test_session_level_set_versus_set_local(self) -> None:
        self.assertEqual(lines_of(self.unprotected, "SEC_RLS_SET_NOT_LOCAL"), [28, 29])
        items = by_rule(self.unprotected)["SEC_RLS_SET_NOT_LOCAL"]
        self.assertTrue(all(f["recommended_test"] and f["verification"] == "needs_runtime_check" for f in items))
        # SET LOCAL and set_config(..., true) at lines 33-34 are correct

    def test_router_level_dependencies_annotated_alias_and_route_dependencies_count_as_signals(self) -> None:
        self.assertEqual(self.protected["findings"], [])
        self.assertEqual(self.protected["facts"]["protected_routers"], 1)


class SecurityInlineTests(ProjectCase):
    ROUTE = """
        from fastapi import APIRouter{extra_import}

        router = APIRouter({router_args})


        @router.get("/orgs/{{org_id}}/items"{route_args})
        async def list_items(org_id: str{params}) -> list:
            return [org_id]
        """

    def _audit_route(self, *, extra_import: str = "", router_args: str = "", route_args: str = "", params: str = ""):
        self.write("app/routes.py", self.ROUTE.format(extra_import=extra_import, router_args=router_args, route_args=route_args, params=params))
        return self.rules()

    def test_unprotected_tenant_route(self) -> None:
        self.assertIn("SEC_TENANT_ID_FROM_REQUEST", self._audit_route())

    def test_depends_on_an_auth_looking_dependency_is_a_signal(self) -> None:
        rules = self._audit_route(extra_import=", Depends", params=", user=Depends(get_current_user)")
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)
        self.assertNotIn("SEC_ROUTE_WITHOUT_AUTH_SIGNAL", rules)

    def test_router_dependencies_are_a_signal(self) -> None:
        rules = self._audit_route(extra_import=", Depends", router_args="dependencies=[Depends(verify_token)]")
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)

    def test_route_decorator_dependencies_are_a_signal(self) -> None:
        rules = self._audit_route(extra_import=", Depends", route_args=", dependencies=[Depends(require_scope)]")
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)

    def test_application_level_signal_in_another_file(self) -> None:
        self.write("app/main.py", """
            from fastapi import Depends, FastAPI
            from app.routes import router
            from app.security import require_user

            app = FastAPI()
            app.include_router(router, dependencies=[Depends(require_user)])
            """)
        rules = self._audit_route()
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)
        self.assertNotIn("SEC_ROUTE_WITHOUT_AUTH_SIGNAL", rules)

    def test_auth_middleware_counts_as_signal(self) -> None:
        self.write("app/main.py", """
            from fastapi import FastAPI
            from app.middleware import AuthenticationMiddleware

            app = FastAPI()
            app.add_middleware(AuthenticationMiddleware)
            """)
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", self._audit_route())

    def test_tenant_param_that_is_not_used_is_not_reported(self) -> None:
        self.write("app/routes.py", """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get("/orgs/{org_id}/ping")
            async def ping(org_id: str) -> dict:
                return {"ok": True}
            """)
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", self.rules())

    def test_set_role_and_set_in_fstring_are_reported_and_markers_in_strings_only(self) -> None:
        self.write("app/rls.py", """
            from sqlalchemy import text

            def set_role(conn, role):
                conn.execute(text(f"SET ROLE {role}"))
                conn.execute(text("SET LOCAL ROLE app_user"))
                conn.execute(text("SET statement_timeout = 1000"))   # no dotted GUC: not tenant context
            """)
        self.assertEqual([f["line"] for f in self.findings("SEC_RLS_SET_NOT_LOCAL")], [4])

    def test_annotated_auth_alias_re_exported_through_a_facade_is_a_signal(self) -> None:
        self.write("src/app/__init__.py", "")
        self.write("src/app/modules/__init__.py", "")
        self.write("src/app/modules/tenants/__init__.py", "")
        self.write("src/app/modules/tenants/dependencies.py", """
            from typing import Annotated
            from fastapi import Depends

            def require_member(tenant_id: str):
                return tenant_id

            def require_owner(ctx=Depends(require_member)):
                return ctx

            MemberDep = Annotated[str, Depends(require_member)]
            OwnerDep = Annotated[str, Depends(require_owner)]
            """)
        self.write("src/app/modules/tenants/public.py", "from app.modules.tenants.dependencies import MemberDep, OwnerDep\n")
        self.write("src/app/modules/ledger/__init__.py", "")
        self.write("src/app/modules/ledger/router.py", """
            from fastapi import APIRouter
            from app.modules.tenants.public import MemberDep, OwnerDep

            router = APIRouter(prefix="/tenants/{tenant_id}/accounts")

            @router.get("/{account_id}")
            async def read(account_id: str, ctx: MemberDep) -> dict:
                return {"id": account_id}

            @router.delete("/{account_id}")
            async def delete(account_id: str, ctx: OwnerDep) -> None:
                return None
            """)
        rules = self.rules()
        self.assertNotIn("SEC_ROUTE_WITHOUT_AUTH_SIGNAL", rules)
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)

    def test_resource_ids_are_not_tenant_identifiers(self) -> None:
        self.write("app/routes.py", """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get("/accounts/{account_id}")
            async def read(account_id: str) -> dict:
                return {"id": account_id}
            """)
        rules = self.rules()
        self.assertNotIn("SEC_TENANT_ID_FROM_REQUEST", rules)
        self.assertIn("SEC_ROUTE_WITHOUT_AUTH_SIGNAL", rules)  # still informational

    def test_non_fastapi_modules_do_not_get_route_findings(self) -> None:
        self.write("app/flask_app.py", """
            from flask import Flask
            app = Flask(__name__)

            @app.get("/orgs/<org_id>")
            def org(org_id):
                return org_id
            """)
        self.assertEqual(self.rules(), [])


if __name__ == "__main__":
    unittest.main()
