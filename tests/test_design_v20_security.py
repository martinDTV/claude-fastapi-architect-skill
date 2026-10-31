"""FastAPI Architect v2.0, increment 5: identity, authorization and tenant isolation.

Schema 1.2 of the design contract (``security.oidc``, the authorization object, ``owned`` entities, the tenant strategy
``shared_schema_application_scoping`` as the new default, the ``principal_claim`` binding, ``multi_tenant.rls`` and
``security.audit``), the generated OIDC validation, the generated authorization policy (per-operation roles in a route
dependency, ownership in services and repository queries), PostgreSQL row-level security as an opt-in strategy, the
guarantees block, the manifest's security record, the auditor on the generated projects (with negative controls) and
the evolution safety rules. Vertical slice 1 (``team-notes``: OIDC + principal_claim + roles_and_ownership, in memory) runs
its generated suite here through the trust model when PyJWT and cryptography are importable; vertical slice 2 (row-level
security on a real PostgreSQL through a restricted role) lives in ``test_design_v20_postgresql.py`` (opt-in).
Nothing generated is executed outside an explicit ``Authorization``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.engine import AuditOptions, audit_project  # noqa: E402
from fastapi_audit.model import TOOL_VERSION  # noqa: E402
from fastapi_audit.remediate.executors import Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy  # noqa: E402
from fastapi_design import evolution  # noqa: E402
from fastapi_design.blueprint import Lineage  # noqa: E402

SPECS = FIXTURES / "design20" / "specs"
EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
ORDERS = EXAMPLES / "specs" / "orders-platform.json"
TEAM_NOTES = EXAMPLES / "specs" / "team-notes.json"
TENANT_PROJECTS = EXAMPLES / "specs" / "tenant-projects.json"
SNAPSHOT_TEAM_NOTES = EXAMPLES / "generated" / "team-notes"
SNAPSHOT_TENANT_PROJECTS = EXAMPLES / "generated" / "tenant-projects"
SNAPSHOT_ORDERS = EXAMPLES / "generated" / "orders-platform"
ROOT_CLI = REPO_ROOT / "scripts" / "design_fastapi.py"
HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "pytest", "httpx", "pydantic_settings"))
HAS_JWT = HAS_FASTAPI and all(importlib.util.find_spec(m) is not None for m in ("jwt", "cryptography"))
PY312 = sys.version_info >= (3, 12)  # the generated code targets 3.12+

MINIMAL_MODULES = {"modules": [{"name": "things", "entities": [{"name": "Thing", "fields": [{"name": "label", "type": "str"}]}]}]}


def _doc(**overrides) -> dict:
    doc = {"schema": "fastapi-architect/spec/1.2", "name": "x", "functional": json.loads(json.dumps(MINIMAL_MODULES))}
    doc.update(overrides)
    return doc


def _errors(doc: dict) -> list:
    spec, errors = design.validate_spec(doc)
    return errors


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-sec-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def target(self, name: str = "out") -> str:
        return str(self.tmp / name)

    def generate(self, spec_path: Path, name: str = "out"):
        spec = design.load_spec(str(spec_path))
        bp = design.build(spec, design.recommend(spec))
        design.generate(spec, bp, self.target(name), tool_version=TOOL_VERSION)
        return spec, bp, Path(self.target(name))


# --------------------------------------------------------------------------- #
# contract (schema 1.2)
# --------------------------------------------------------------------------- #


class SecurityContractTests(unittest.TestCase):
    def test_schema_12_security_is_normalised_and_round_trips(self) -> None:
        spec = design.load_spec(str(TEAM_NOTES))
        self.assertEqual(spec.schema, "fastapi-architect/spec/1.2")
        self.assertEqual(spec.authz_model, "roles_and_ownership")
        self.assertIsNone(spec.authz_declared)
        self.assertTrue(spec.with_roles and spec.with_ownership)
        self.assertEqual([e.name for _m, e in spec.owned_entities()], ["Note"])
        o = spec.oidc
        assert o is not None
        self.assertEqual((o["issuer"], o["audience"], o["algorithms"], o["leeway_s"], o["jwks_cache_s"]), ("https://login.example.com/", "team-notes-api", ["RS256"], 30, 300))
        self.assertEqual(o["claims"], {"subject": "sub", "tenant": "workspace_id", "roles": "roles"})
        self.assertEqual(spec.security["multi_tenant"]["binding"], "principal_claim")
        self.assertEqual(spec.security["multi_tenant"]["rls"], {"app_role": "team_notes_app", "context_key": "app.tenant_id"})
        self.assertTrue(spec.security["audit"]["authorization_decisions"])
        self.assertFalse(spec.rls)  # in memory: application scoping
        document = spec.security_document()
        self.assertNotIn("declared", document["authorization"])
        self.assertEqual(document["authorization"]["model"], "roles_and_ownership")
        again, errors = design.validate_spec({**spec.document, "security": document})
        self.assertEqual(errors, [])
        self.assertEqual(again.security, spec.security)
        self.assertTrue(spec.to_dict()["functional"]["modules"][0]["entities"][0]["owned"])

    def test_defaults_of_schema_12(self) -> None:
        spec, errors = design.validate_spec(_doc(security={"authentication": "oauth2_jwt", "oidc": {"issuer": "https://idp.example.com", "audience": "api"}}))
        self.assertEqual(errors, [])
        assert spec is not None
        self.assertEqual(spec.oidc["jwks_url"], "https://idp.example.com/.well-known/jwks.json")
        self.assertEqual(spec.oidc["algorithms"], ["RS256"])
        self.assertEqual(spec.provenance["spec.security.oidc.algorithms"], "assumed")
        self.assertEqual(spec.authz_model, "none")
        self.assertEqual(spec.provenance["spec.security.authorization"], "assumed")
        spec, errors = design.validate_spec(_doc(persistence={"kind": "postgresql", "migrations": True}, security={"authentication": "api_key", "multi_tenant": {"enabled": True, "binding": "api_key_registry"}}))
        self.assertEqual(errors, [])
        assert spec is not None
        self.assertEqual(spec.security["multi_tenant"]["strategy"], "shared_schema_application_scoping")
        self.assertEqual(spec.provenance["spec.security.multi_tenant.strategy"], "assumed")
        self.assertFalse(spec.rls)

    def test_legacy_contracts_keep_their_semantics(self) -> None:
        """A 1.1 document that declares shared_schema_rls still gets application scoping, a pending decision and the legacy
        authorization string back; the engine never upgrades a contract's meaning behind its author's back."""
        spec = design.load_spec(str(ORDERS))
        self.assertEqual(spec.schema, "fastapi-architect/spec/1.1")
        self.assertEqual(spec.security["multi_tenant"]["strategy"], "shared_schema_rls")
        self.assertFalse(spec.rls)
        self.assertEqual(spec.authz_model, "none")
        self.assertEqual(spec.authz_declared, "resource")
        self.assertEqual(spec.security_document()["authorization"], "resource")
        self.assertEqual(spec.to_dict()["security"]["authorization"], "resource")
        g = design.security_guarantees(spec)
        self.assertEqual(g["multi_tenant"]["strategy_implemented"], "shared_schema_application_scoping")
        self.assertFalse(g["multi_tenant"]["row_level_security"])
        self.assertTrue(g["resource_authorization"].startswith("not generated"))
        self.assertIn("row-level security", g["not_claimed"])
        rec = design.recommend(spec)
        self.assertTrue(any("1.0/1.1 contract" in p and "shared_schema_rls" in p for p in rec.pending), rec.pending)
        self.assertTrue(any("authorization 'resource' is a declaration" in p for p in rec.pending), rec.pending)
        self.assertTrue(design.validate_spec(_doc(schema="fastapi-architect/spec/1.1", security={"authentication": "api_key", "multi_tenant": {"enabled": True}}))[0].security["multi_tenant"]["strategy"] == "shared_schema_rls")

    def test_impossible_or_unsupported_combinations_are_rejected_with_their_paths(self) -> None:
        cases = [
            ({"authentication": "api_key", "multi_tenant": {"enabled": True, "strategy": "shared_schema_rls", "binding": "api_key_registry"}}, {"kind": "memory"}, "spec.security.multi_tenant.strategy: 'shared_schema_rls' is PostgreSQL row-level security; persistence.kind is 'memory'"),
            ({"authentication": "api_key", "multi_tenant": {"enabled": True, "binding": "principal_claim"}}, {"kind": "memory"}, "spec.security.multi_tenant.binding: 'principal_claim' takes the tenant from a verified token claim and needs authentication 'oauth2_jwt'"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a"}, "multi_tenant": {"enabled": True, "binding": "api_key_registry"}}, {"kind": "memory"}, "spec.security.multi_tenant.binding: 'api_key_registry' binds API keys to tenants and needs authentication 'api_key'"),
            ({"authentication": "api_key", "authorization": {"model": "roles", "roles": ["admin"]}}, {"kind": "memory"}, "needs a caller identity; a shared API key has none"),
            ({"authentication": "session", "authorization": {"model": "roles", "roles": ["admin"]}}, {"kind": "memory"}, "cannot be generated with authentication 'session'"),
            ({"authentication": "none", "authorization": {"model": "roles", "roles": ["admin"]}}, {"kind": "memory"}, "needs an authenticated principal (authentication is 'none')"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a", "algorithms": ["none"]}}, {"kind": "memory"}, "spec.security.oidc.algorithms: none refused"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a", "jwks_url": "http://i.example/jwks"}}, {"kind": "memory"}, "spec.security.oidc.jwks_url"),
            ({"authentication": "oauth2_jwt"}, {"kind": "memory"}, "spec.security.oidc: required for authentication 'oauth2_jwt'"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a", "leeway_s": 900}}, {"kind": "memory"}, "spec.security.oidc.leeway_s"),
            ({"authentication": "api_key", "multi_tenant": {"enabled": True, "binding": "api_key_registry", "rls": {"app_role": "x"}}}, {"kind": "postgresql", "migrations": True}, "spec.security.multi_tenant.rls: only valid with strategy 'shared_schema_rls'"),
            ({"authentication": "api_key", "multi_tenant": {"enabled": True, "strategy": "shared_schema_rls", "binding": "api_key_registry", "rls": {"context_key": "no-dot"}}}, {"kind": "postgresql", "migrations": True}, "spec.security.multi_tenant.rls.context_key"),
            ({"authentication": "none", "audit": {"authorization_decisions": True}}, {"kind": "memory"}, "spec.security.audit.authorization_decisions"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a"}, "authorization": {"model": "ownership", "ownership": {"admin_roles": []}}}, {"kind": "memory"}, "'ownership' needs at least one entity with owned: true"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a"}, "authorization": {"model": "roles", "roles": ["a"], "ownership": {"visibility": "owner"}}}, {"kind": "memory"}, "spec.security.authorization.ownership: only valid with model ownership or roles_and_ownership"),
            ({"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a"}, "authorization": {"model": "none", "roles": ["a"]}}, {"kind": "memory"}, "roles are declared but model 'none' does not use them"),
        ]
        for security, persistence, fragment in cases:
            with self.subTest(fragment=fragment):
                errors = _errors(_doc(security=security, persistence=persistence))
                self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_owned_entities_need_an_ownership_model_and_vice_versa(self) -> None:
        owned = {"modules": [{"name": "things", "entities": [{"name": "Thing", "owned": True, "fields": [{"name": "label", "type": "str"}]}]}]}
        errors = _errors(_doc(functional=owned, security={"authentication": "oauth2_jwt", "oidc": {"issuer": "https://i.example", "audience": "a"}}))
        self.assertTrue(any("owned entities (things.Thing) need security.authorization.model ownership or roles_and_ownership" in e for e in errors), errors)
        errors = _errors(_doc(functional={"modules": [{"name": "things", "entities": [{"name": "Thing", "fields": [{"name": "owner_id", "type": "str"}]}]}]}))
        self.assertTrue(any("owner_id" in e and "generated automatically" in e for e in errors), errors)

    def test_fixture_contracts_of_schema_12(self) -> None:
        for name in ("oidc_owner_scoped", "rls_claim_binding"):
            spec = design.load_spec(str(SPECS / "valid" / f"{name}.json"))
            self.assertIsNone(design.generation_refusal(spec), name)
        journal = design.load_spec(str(SPECS / "valid" / "oidc_owner_scoped.json"))
        self.assertFalse(journal.multi_tenant)
        self.assertEqual(journal.security["authorization"]["ownership"]["visibility"], "owner")
        self.assertEqual(journal.oidc["algorithms"], ["ES256"])
        self.assertEqual(journal.oidc["claims"]["roles"], "https://example.org/roles")
        fleet = design.load_spec(str(SPECS / "valid" / "rls_claim_binding.json"))
        self.assertTrue(fleet.rls)
        self.assertEqual(fleet.security["multi_tenant"]["rls"], {"app_role": "fleet_api", "context_key": "fleet.current"})
        for name in ("security12_identity", "security12_oidc", "security12_policy", "security12_in_11"):
            doc = json.loads((SPECS / "invalid" / f"{name}.json").read_text(encoding="utf-8"))
            errors = _errors(doc)
            for expected in doc["x-expect"]:
                self.assertTrue(any(expected in e for e in errors), f"{name}: {expected!r} not in {errors}")

    def test_generation_refusals_name_the_missing_piece(self) -> None:
        pending = design.load_spec(str(SPECS / "valid" / "oauth_pending.json"))
        refusal = design.generation_refusal(pending)
        self.assertIn("schema 1.2", refusal)
        self.assertIn("security.oidc", refusal)
        session, errors = design.validate_spec(_doc(security={"authentication": "session"}))
        self.assertEqual(errors, [])
        self.assertIn("'session'", design.generation_refusal(session))
        unbound, errors = design.validate_spec(_doc(persistence={"kind": "postgresql", "migrations": True}, security={"authentication": "api_key", "multi_tenant": {"enabled": True}}))
        self.assertEqual(errors, [])
        self.assertIn("principal_claim", design.generation_refusal(unbound))

    def test_guarantees_state_identity_authorization_and_isolation_without_overclaiming(self) -> None:
        notes = design.security_guarantees(design.load_spec(str(TEAM_NOTES)))
        self.assertTrue(notes["authentication"].startswith("oidc:"))
        self.assertIn("HS*/none refused", notes["authentication"])
        self.assertEqual(notes["identity"]["principal"], "per token (verified claims only)")
        self.assertEqual(notes["identity"]["tenant_source"], "claim workspace_id (uuid)")
        self.assertTrue(notes["resource_authorization"].startswith("roles_and_ownership:"))
        self.assertIn("create needs admin/editor", notes["resource_authorization"])
        self.assertIn("reads are tenant-wide, update/delete need the owner or an admin role (admin)", notes["resource_authorization"])
        self.assertEqual(notes["multi_tenant"]["strategy_implemented"], "shared_schema_application_scoping")
        self.assertFalse(notes["multi_tenant"]["row_level_security"])
        self.assertIn("verified workspace_id claim", notes["multi_tenant"]["principal_tenant_binding"])
        self.assertIn("row-level security", notes["not_claimed"])
        self.assertNotIn("resource-level authorization", notes["not_claimed"])
        self.assertNotIn("OAuth2/JWT authentication", notes["not_claimed"])
        self.assertIn("refresh tokens, token issuance or an authorization server", notes["not_claimed"])
        self.assertIn("denials are logged", notes["audit_logging"])
        projects = design.security_guarantees(design.load_spec(str(TENANT_PROJECTS)))
        self.assertEqual(projects["multi_tenant"]["strategy_implemented"], "shared_schema_rls")
        self.assertTrue(projects["multi_tenant"]["row_level_security"])
        self.assertEqual(projects["multi_tenant"]["rls"]["context_scope"], "transaction (set_config third argument true); never SET at session level")
        self.assertTrue(any("ENABLE + FORCE" in s for s in projects["multi_tenant"]["storage_isolation"]))
        self.assertNotIn("row-level security", projects["not_claimed"])
        self.assertIn("cross-tenant administration", projects["not_claimed"])
        self.assertEqual(projects["identity"]["principal"], "per API key (grant)")
        self.assertIn("503", projects["persistence"]["infrastructure_failure"])
        md = design.render_markdown(design.run_design(str(TENANT_PROJECTS), design.DesignOptions(validate_only=True)))
        self.assertIn("row-level security: True", md)

    def test_advisor_explains_the_security_design(self) -> None:
        rec = design.recommend(design.load_spec(str(TEAM_NOTES)))
        self.assertTrue(any("OIDC bearer tokens from https://login.example.com/" in n for n in rec.security_notes), rec.security_notes)
        self.assertTrue(any("ownership of notes.Note" in n for n in rec.security_notes), rec.security_notes)
        self.assertTrue(any("binding principal_claim" in n for n in rec.security_notes))
        self.assertFalse(any("authorization" in p for p in rec.pending), rec.pending)
        rec = design.recommend(design.load_spec(str(TENANT_PROJECTS)))
        self.assertTrue(any("PostgreSQL row-level security (ENABLE + FORCE" in n for n in rec.security_notes), rec.security_notes)
        self.assertEqual(rec.pending, [])


# --------------------------------------------------------------------------- #
# blueprint: generated identity, policy and RLS code
# --------------------------------------------------------------------------- #


class SecurityBlueprintTests(TempDirCase):
    def test_oidc_validation_module_and_its_wiring(self) -> None:
        spec = design.load_spec(str(TEAM_NOTES))
        bp = design.build(spec, design.recommend(spec))
        security = bp.files["src/team_notes/core/security.py"]
        self.assertIn('ALGORITHMS = ("RS256",)', security)
        self.assertNotIn('"HS256"', security)
        self.assertIn('if header.get("alg") not in ALGORITHMS:', security)
        self.assertIn('REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "workspace_id")', security)
        self.assertIn('options={"require": list(REQUIRED_CLAIMS)}', security)
        self.assertIn("audience=settings.oidc_audience", security)
        self.assertIn("issuer=settings.oidc_issuer", security)
        self.assertIn("MIN_REFRESH_INTERVAL_S = 30", security)
        self.assertIn('raise RuntimeError("OIDC_JWKS_URL must be an https URL")', security)
        self.assertIn("is accepted only when ENVIRONMENT is 'test'", security)
        self.assertIn("HTTP_503_SERVICE_UNAVAILABLE", security)
        self.assertIn('"WWW-Authenticate": "Bearer"', security)
        self.assertIn("def require_tenant_member(workspace_id: UUID, principal: AuthenticatedPrincipal) -> Principal:", security)
        self.assertIn("tenant_id = UUID(str(raw_tenant))", security)
        self.assertIn('"pyjwt[crypto]>=2.9,<3"', bp.files["pyproject.toml"])
        config = bp.files["src/team_notes/core/config.py"]
        for field in ("oidc_issuer", "oidc_audience", "oidc_jwks_url", "oidc_jwks_json", "oidc_jwks_cache_s", "oidc_leeway_s"):
            self.assertIn(field, config)
        self.assertNotIn("oidc_algorithms", config)  # not configurable: an environment cannot lower the accepted algorithms
        self.assertIn("app.state.jwks = JwksSource(settings)", bp.files["src/team_notes/main.py"])
        self.assertIn("dependencies=[Depends(require_bearer_token)]", bp.files["src/team_notes/api/router.py"])
        auth_tests = bp.files["tests/test_auth.py"]
        for name in ("test_a_token_with_altered_claims_keeps_its_signature_but_is_rejected", "test_a_symmetric_signature_with_the_public_key_is_rejected", "test_an_unsigned_token_is_rejected", "test_an_unknown_key_id_or_a_missing_key_id_is_rejected", "test_a_token_signed_by_another_key_with_the_same_key_id_is_rejected", "test_a_token_without_a_usable_tenant_claim_is_401", "test_a_member_of_another_tenant_is_403_not_401", "test_a_static_jwks_is_refused_outside_the_test_environment", "test_an_unreachable_provider_is_503_not_401"):
            self.assertIn(f"def {name}(", auth_tests)
        self.assertIn('"workspace_id"', bp.files[".audit-fastapi.json"])
        self.assertIn('"mutation_authorization_required"', bp.files[".audit-fastapi.json"])

    def test_authorization_policy_reaches_routes_services_and_repositories(self) -> None:
        spec = design.load_spec(str(TEAM_NOTES))
        bp = design.build(spec, design.recommend(spec))
        authz = bp.files["src/team_notes/core/authz.py"]
        self.assertIn('"create": frozenset({"admin", "editor"}),', authz)
        self.assertIn('"delete": frozenset({"admin", "editor"}),', authz)
        self.assertNotIn('"get":', authz)  # open to every authenticated member
        self.assertIn('ADMIN_ROLES: frozenset[str] = frozenset({"admin"})', authz)
        self.assertIn('OWNER_VISIBILITY = "tenant"', authz)
        self.assertIn('LOG = logging.getLogger("team_notes.authz")', authz)
        self.assertIn("def dependency(principal: TenantPrincipalDep) -> Principal:", authz)
        router = bp.files["src/team_notes/api/notes.py"]
        self.assertIn("async def create_note(principal: CanCreate, payload: NoteCreate, service: NoteServiceDep)", router)
        self.assertIn("service.update(principal, note_id, payload)", router)
        self.assertNotIn("workspace_id: UUID", router)
        service = bp.files["src/team_notes/services/notes.py"]
        self.assertIn("            workspace_id=principal.tenant_id,\n            owner_id=principal.subject,\n            title=data.title,", service)
        self.assertIn("owner=read_scope(principal)", service)
        self.assertIn('ensure_owner(principal, note.owner_id, "update", "Note")', service)
        self.assertIn('ensure_owner(principal, note.owner_id, "delete", "Note")', service)
        self.assertNotIn("ensure_owner(principal, tag", service)  # Tag is not owned
        repository = bp.files["src/team_notes/repositories/notes.py"]
        self.assertIn("async def get(self, workspace_id: UUID, note_id: UUID, owner: str | None = None) -> Note | None:", repository)
        self.assertIn("(owner is None or item.owner_id == owner)", repository)
        self.assertIn("async def get(self, workspace_id: UUID, tag_id: UUID) -> Tag | None:", repository)
        self.assertIn("    owner_id: str\n", bp.files["src/team_notes/schemas/notes.py"])
        self.assertIn("class ForbiddenError(DomainError):", bp.files["src/team_notes/core/errors.py"])
        tests = bp.files["tests/test_notes.py"]
        for name in ("test_create_note_needs_a_permitted_role", "test_note_records_the_creating_principal_as_owner_and_never_takes_it_from_the_client", "test_another_member_reads_note_of_a_colleague", "test_another_member_cannot_update_note_of_a_colleague", "test_another_member_cannot_delete_note_of_a_colleague", "test_an_admin_can_update_note_of_another_member", "test_note_of_another_tenant_is_not_found_through_the_callers_tenant", "test_get_note_with_a_malformed_id_is_422", "test_note_routes_refuse_another_tenant_for_the_same_principal"):
            self.assertIn(f"def {name}(", tests)
        conftest = bp.files["tests/conftest.py"]
        self.assertIn('"limited": (TENANT_A, []),', conftest)
        self.assertIn('"carol": (TENANT_B, ["admin", "editor", "viewer"]),', conftest)
        self.assertIn("def mint(", conftest)

    def test_owner_visibility_changes_reads_and_404s(self) -> None:
        spec = design.load_spec(str(SPECS / "valid" / "oidc_owner_scoped.json"))
        bp = design.build(spec, design.recommend(spec))
        self.assertIn('OWNER_VISIBILITY = "owner"', bp.files["src/journal/core/authz.py"])
        tests = bp.files["tests/test_entries.py"]
        self.assertIn("def test_another_member_cannot_see_entry_of_a_colleague(", tests)
        self.assertIn("def test_list_entries_is_owner_scoped(", tests)
        self.assertIn("def test_an_admin_sees_entry_of_another_member(", tests)
        self.assertIn(".status_code == 404  # visibility: owner", tests)
        self.assertIn("async def get(self, entry_id: UUID, owner: str | None = None) -> Entry | None:", bp.files["src/journal/repositories/entries.py"])
        self.assertIn("ec.generate_private_key(ec.SECP256R1())", bp.files["tests/conftest.py"])
        self.assertIn('ROLES_CLAIM = "https://example.org/roles"', bp.files["src/journal/core/security.py"])
        self.assertNotIn("require_tenant_member", bp.files["src/journal/core/security.py"])  # single tenant

    def test_rls_is_generated_only_for_schema_12_postgresql_contracts(self) -> None:
        spec = design.load_spec(str(TENANT_PROJECTS))
        bp = design.build(spec, design.recommend(spec))
        migration = next(content for path, content in bp.files.items() if path.startswith("migrations/versions/"))
        for fragment in (
            "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            "CREATE POLICY {table}_tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})",
            "tenant_id = NULLIF(current_setting('{CONTEXT_KEY}', true), '')::uuid",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {role}",
            "GRANT USAGE ON SCHEMA public TO {_app_role()}",
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name",
            "would bypass row-level security",
            'APP_ROLE = os.environ.get("TENANT_PROJECTS_APP_ROLE", "tenant_projects_app")',
            '_protect("projects")',
            '_protect("tasks")',
        ):
            self.assertIn(fragment, migration)
        self.assertNotIn('op.execute("CREATE ROLE', migration)  # roles are provisioning, never created with a default password
        self.assertNotIn("SET app.tenant_id", migration)
        db = bp.files["src/tenant_projects/core/db.py"]
        self.assertIn('await session.execute(text("SELECT set_config(:key, :tenant, true)"), params)', db)
        self.assertIn('"tenant": str(principal.tenant_id)', db)
        self.assertIn("TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]", db)
        self.assertNotIn("SET LOCAL", db)
        self.assertNotIn("request.headers", db)
        self.assertIn("def get_task_repository(session: TenantSessionDep)", bp.files["src/tenant_projects/modules/tasks/dependencies.py"])
        self.assertIn('owner = os.environ.get("TENANT_PROJECTS_MIGRATIONS_DATABASE_URL")', bp.files["migrations/env.py"])
        conftest = bp.files["tests/conftest.py"]
        self.assertIn("def owner_url() -> str:", conftest)
        self.assertIn("def migrated(owner_url: str):", conftest)
        self.assertIn("def app(migrated: str, database_url: str):", conftest)
        rls_tests = bp.files["tests/test_rls.py"]
        for name in ("test_every_tenant_table_has_rls_enabled_forced_and_a_policy", "test_the_application_role_is_restricted", "test_without_tenant_context_no_row_is_visible", "test_without_tenant_context_an_insert_is_rejected", "test_with_tenant_context_only_that_tenants_rows_are_visible", "test_with_tenant_context_a_row_of_another_tenant_cannot_be_written", "test_tenant_context_does_not_survive_commit_rollback_or_the_pool", "test_rollback_leaves_no_row", "test_an_invalid_or_empty_tenant_context_fails_closed", "test_the_application_role_cannot_lift_the_protection", "test_concurrent_requests_of_two_tenants_never_see_each_other", "test_an_unreachable_database_is_503_not_401_or_404"):
            self.assertIn(f"def {name}(", rls_tests)
        self.assertIn("pool_size=1, max_overflow=0", rls_tests)
        self.assertIn("anyio.create_task_group()", rls_tests)
        self.assertTrue(all(m["row_level_security"] for m in bp.migrations))
        self.assertIn("TENANT_PROJECTS_APP_ROLE=tenant_projects_app", bp.files[".env.example"])
        self.assertIn("TENANT_PROJECTS_MIGRATIONS_DATABASE_URL=", bp.files[".env.example"])
        self.assertIn("@app.exception_handler(OperationalError)", bp.files["src/tenant_projects/core/errors.py"])
        # the 1.1 orders contract declares the same strategy and gets none of it
        orders = design.load_spec(str(ORDERS))
        old = design.build(orders, design.recommend(orders))
        self.assertNotIn("tests/test_rls.py", old.files)
        self.assertNotIn("ROW LEVEL SECURITY", next(c for p, c in old.files.items() if p.startswith("migrations/versions/")))
        self.assertNotIn("get_tenant_session", old.files["src/orders_platform/core/db.py"])
        self.assertFalse(any(m["row_level_security"] for m in old.migrations))
        self.assertIn("class ApiKeyGrant(BaseModel):", old.files["src/orders_platform/core/config.py"])
        self.assertIn('return {key: ({"tenant_id": grant} if isinstance(grant, str) else grant) for key, grant in value.items()}', old.files["src/orders_platform/core/config.py"])

    def test_rls_with_the_claim_binding_sets_the_context_from_the_token(self) -> None:
        spec = design.load_spec(str(SPECS / "valid" / "rls_claim_binding.json"))
        bp = design.build(spec, design.recommend(spec))
        self.assertIn('TENANT_CONTEXT_KEY = "fleet.current"', bp.files["src/fleet/core/db.py"])
        self.assertIn("fleet_id = NULLIF(current_setting('{CONTEXT_KEY}', true), '')::uuid", next(c for p, c in bp.files.items() if p.startswith("migrations/versions/")))
        self.assertIn("from tests.conftest import TENANT_A, TENANT_B, credentials, jwks_json, run", bp.files["tests/test_rls.py"])
        self.assertIn('oidc_jwks_json=jwks_json(), database_url=url', bp.files["tests/test_rls.py"])
        self.assertIn('TENANT_CLAIM = "fleet_id"', bp.files["src/fleet/core/security.py"])

    def test_evolution_lineage_protects_only_the_new_tables(self) -> None:
        spec = design.load_spec(str(TENANT_PROJECTS))
        rec = design.recommend(spec)
        doc = json.loads(json.dumps(spec.document))
        doc["functional"]["modules"].append({"name": "milestones", "depends_on": ["projects"], "entities": [{"name": "Milestone", "fields": [{"name": "project_id", "type": "uuid", "references": "projects.Project"}, {"name": "title", "type": "str", "max_length": 120}], "operations": ["create", "get", "list"]}]})
        evolved, errors = design.validate_spec(doc)
        self.assertEqual(errors, [])
        head = design.build(spec, rec).migrations[0]
        lineage = Lineage(head=head["revision"], files=[head["file"]], tables=head["tables"], revisions=[head], baseline=spec)
        bp = design.build(evolved, design.recommend(evolved), lineage)
        delta = next(content for path, content in bp.files.items() if path.startswith("migrations/versions/") and path != head["file"])
        self.assertIn('_protect("milestones")', delta)
        self.assertNotIn('_protect("projects")', delta)
        self.assertIn("FORCE ROW LEVEL SECURITY", delta)
        self.assertIn('"milestones"', bp.files["tests/test_rls.py"])

    @unittest.skipUnless(shutil.which("ruff") or shutil.which("uvx"), "ruff is not available")
    def test_generated_security_projects_pass_their_own_ruff_configuration(self) -> None:
        for spec_path in (TEAM_NOTES, TENANT_PROJECTS, SPECS / "valid" / "oidc_owner_scoped.json", SPECS / "valid" / "rls_claim_binding.json"):
            _spec, _bp, target = self.generate(spec_path, spec_path.stem)
            command = [shutil.which("ruff"), "check", "."] if shutil.which("ruff") else [shutil.which("uvx"), "ruff", "check", "."]
            proc = subprocess.run(command, cwd=str(target), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{spec_path.name}:\n{proc.stdout}{proc.stderr}")


# --------------------------------------------------------------------------- #
# vertical slice 1: authorization and OIDC through the trust model
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAS_JWT and PY312, "fastapi, pytest, httpx, PyJWT and cryptography are needed by the generated suite (python >= 3.12)")
class AuthorizationSliceTests(TempDirCase):
    def _run(self, spec_path: Path, name: str) -> dict:
        command = f"{sys.executable} -m pytest -q -p no:cacheprovider"
        target = self.target(name)
        options = design.DesignOptions(target=target, authorization=Authorization(execution=True, trust="fixture", scope=target, declared_by="tests: fixture generated by this suite"), policy=ExecutionPolicy(authorized=True, command=command, timeout_s=600), test_command=command)
        report = design.run_design(str(spec_path), options)
        tests = report["verification"]["tests"]
        self.assertTrue(tests["ran"], tests)
        run = tests["run"]
        self.assertEqual(run["outcome"], "passed", (run.get("stdout_tail") or "")[-4000:])
        self.assertEqual(run["runner"], "pytest-junit")
        self.assertTrue(run["evidence"]["matched"])
        self.assertEqual((run["counts"]["failed"], run["counts"]["errors"], run["counts"]["skipped"]), (0, 0, 0), run["counts"])
        self.assertEqual(tests["workspace"]["python_files_changed_by_the_run"], [])
        self.assertEqual(set(design.verify_manifest(target)["status"].values()), {"identical"})
        return report

    def test_team_notes_passes_its_generated_suite(self) -> None:
        report = self._run(TEAM_NOTES, "team-notes")
        run = report["verification"]["tests"]["run"]
        self.assertGreaterEqual(run["counts"]["passed"], 50)
        executed = {e.split("::")[-1] for e in run["evidence"]["tests"]["passed"]}
        for expected in (
            "test_create_note",  # legitimate access
            "test_another_member_reads_note_of_a_colleague",  # same tenant, per policy (visibility tenant)
            "test_another_member_cannot_update_note_of_a_colleague",  # modification without permission: 403
            "test_another_member_cannot_delete_note_of_a_colleague",
            "test_an_admin_can_update_note_of_another_member",  # exceptional admin operation inside the tenant
            "test_create_note_needs_a_permitted_role",  # forbidden operation: 403
            "test_note_routes_refuse_another_tenant_for_the_same_principal",  # other tenant: 403
            "test_note_of_another_tenant_is_not_found_through_the_callers_tenant",  # tampered identifier: 404
            "test_get_note_returns_the_created_one_and_404_for_unknown",  # nonexistent resource
            "test_get_note_with_a_malformed_id_is_422",
            "test_note_records_the_creating_principal_as_owner_and_never_takes_it_from_the_client",
            "test_note_routes_require_a_valid_bearer_token",  # invalid credential: 401
            "test_a_token_with_altered_claims_keeps_its_signature_but_is_rejected",
            "test_an_expired_token_is_rejected_beyond_the_leeway",
            "test_a_symmetric_signature_with_the_public_key_is_rejected",
            "test_an_unsigned_token_is_rejected",
            "test_an_unknown_key_id_or_a_missing_key_id_is_rejected",
            "test_a_token_signed_by_another_key_with_the_same_key_id_is_rejected",
            "test_wrong_audience_or_issuer_is_rejected",
            "test_a_token_without_a_usable_tenant_claim_is_401",
            "test_a_member_of_another_tenant_is_403_not_401",
            "test_a_static_jwks_is_refused_outside_the_test_environment",
            "test_an_unreachable_provider_is_503_not_401",
        ):
            self.assertIn(expected, executed)
        self.assertEqual(report["verification"]["audit"]["findings_total"], 0, report["verification"]["audit"]["findings"])
        manifest = design.verify_manifest(self.target("team-notes"))
        self.assertEqual(manifest["manifest"]["security"], {"authentication": "oauth2_jwt", "identity": "per_caller", "authorization_model": "roles_and_ownership", "authorization_declared": None, "tenant_strategy_declared": "shared_schema_application_scoping", "tenant_strategy_implemented": "shared_schema_application_scoping", "tenant_binding": "principal_claim", "row_level_security": False})

    def test_owner_scoped_single_tenant_journal_passes_its_generated_suite(self) -> None:
        report = self._run(SPECS / "valid" / "oidc_owner_scoped.json", "journal")
        executed = {e.split("::")[-1] for e in report["verification"]["tests"]["run"]["evidence"]["tests"]["passed"]}
        for expected in ("test_another_member_cannot_see_entry_of_a_colleague", "test_list_entries_is_owner_scoped", "test_an_admin_sees_entry_of_another_member", "test_an_admin_can_update_entry_of_another_member", "test_create_entry_needs_a_permitted_role", "test_roles_may_come_as_a_space_separated_string"):
            self.assertIn(expected, executed)


# --------------------------------------------------------------------------- #
# the auditor on the generated projects, with negative controls
# --------------------------------------------------------------------------- #


class SecurityAuditIntegrationTests(TempDirCase):
    def _audit(self, target: Path) -> dict:
        return audit_project(str(target), options=AuditOptions())

    def _routes(self, report: dict) -> list:
        return [r for r in (report.get("contracts") or {}).get("routes", []) if isinstance(r, dict)]

    def test_generated_projects_show_their_authorization_to_the_auditor(self) -> None:
        for spec_path, module_route in ((TEAM_NOTES, "/tenants/{workspace_id}/notes/{note_id}"), (TENANT_PROJECTS, "/tenants/{tenant_id}/tasks/{task_id}")):
            _spec, _bp, target = self.generate(spec_path, spec_path.stem)
            report = self._audit(target)
            active = [f for f in report["findings"] if not f.get("suppressed")]
            self.assertTrue(all(f["severity"] == "info" for f in active), [(f["rule"], f["severity"]) for f in active])
            summary = report["contracts"]["summary"]
            self.assertEqual((summary["authorization_unknown"], summary["unauthenticated_not_public"], summary["exposure_undetermined"]), (0, 0, 0), summary)
            protected = [r for r in self._routes(report) if r.get("authentication") == "present"]
            self.assertTrue(protected)
            for r in protected:
                self.assertIn("role", r["authorization"], r)
                if r.get("resource_params"):
                    self.assertIn("principal_with_resource", r["authorization"], r)
            self.assertTrue(any(r.get("full_path") == module_route for r in protected), [r.get("full_path") for r in protected])

    def test_negative_control_a_route_that_loses_its_policy_and_principal_is_reported(self) -> None:
        """The generated .audit-fastapi.json declares mutation_authorization_required: stripping the policy dependency and the
        principal from one generated route is caught through that declared contract, not through a naming guess."""
        _spec, _bp, target = self.generate(TEAM_NOTES, "notes")
        router = target / "src" / "team_notes" / "api" / "notes.py"
        source = router.read_text(encoding="utf-8")
        old = "async def update_note(principal: CanUpdate, note_id: UUID, payload: NoteUpdate, service: NoteServiceDep) -> NoteRead:\n    return NoteRead.model_validate(await service.update(principal, note_id, payload))"
        self.assertIn(old, source)
        mutated = source.replace(old, "async def update_note(workspace_id: UUID, note_id: UUID, payload: NoteUpdate, service: NoteServiceDep) -> NoteRead:\n    return NoteRead.model_validate(await service.update(workspace_id, note_id, payload))").replace("CanList, CanUpdate", "CanList")
        router.write_text(mutated, encoding="utf-8")
        report = self._audit(target)
        rules = {f["rule"] for f in report["findings"] if f["file"].endswith("api/notes.py")}
        self.assertIn("CONTRACT_MUTATION_AUTHZ_REQUIRED", rules, [(f["rule"], f["file"], f["line"]) for f in report["findings"]])
        patch = next(r for r in self._routes(report) if r.get("method") == "PATCH" and "notes" in (r.get("full_path") or ""))
        self.assertEqual(patch["authorization"], [])
        self.assertEqual(patch["evidence"], "unknown")

    def test_negative_control_session_level_set_is_reported(self) -> None:
        _spec, _bp, target = self.generate(TENANT_PROJECTS, "projects")
        db = target / "src" / "tenant_projects" / "core" / "db.py"
        source = db.read_text(encoding="utf-8")
        old = '    await session.execute(text("SELECT set_config(:key, :tenant, true)"), params)'
        self.assertIn(old, source)
        db.write_text(source.replace(old, '    await session.execute(text(f"SET app.tenant_id = \'{principal.tenant_id}\'"))'), encoding="utf-8")
        report = self._audit(target)
        rules = {f["rule"] for f in report["findings"] if f["file"].endswith("core/db.py")}
        self.assertIn("SEC_RLS_SET_NOT_LOCAL", rules, [(f["rule"], f["file"], f["line"]) for f in report["findings"]])

    def test_the_auditor_does_not_invent_guarantees_from_names(self) -> None:
        """The report of a generated project never says 'row-level security' or 'resource authorization' because of a
        comment or a file name: the guarantees block comes from the contract, the audit from the code."""
        spec = design.load_spec(str(ORDERS))
        g = design.security_guarantees(spec)
        self.assertFalse(g["multi_tenant"]["row_level_security"])
        _spec, _bp, target = self.generate(ORDERS, "orders")
        (target / "src" / "orders_platform" / "core" / "rls_policies.py").write_text('"""Row-level security policies are applied here (they are not)."""\n', encoding="utf-8")
        report = self._audit(target)
        self.assertNotIn("row_level_security", json.dumps(report.get("contracts") or {}))


# --------------------------------------------------------------------------- #
# evolution safety and the manifest
# --------------------------------------------------------------------------- #


class SecurityEvolutionTests(TempDirCase):
    def test_security_changes_are_not_evolution_operations(self) -> None:
        for key in ("security", "persistence"):
            contract, errors = evolution.validate_evolution({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": "."}, key: {"authentication": "oauth2_jwt"}})
            self.assertIsNone(contract)
            self.assertTrue(any(f"evolution.{key}: not an evolution operation" in e for e in errors), errors)
            self.assertTrue(any("never done by an evolution" in e for e in errors))

    def _copy(self, snapshot: Path, name: str) -> Path:
        project = self.tmp / name
        shutil.copytree(snapshot, project, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache", ".venv"))
        return project

    def test_adding_a_module_to_an_rls_project_protects_the_new_table_and_leaves_core_files_alone(self) -> None:
        project = self._copy(SNAPSHOT_TENANT_PROJECTS, "projects")
        contract_path = self.tmp / "evolve.json"
        contract_path.write_text(json.dumps({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "milestones", "depends_on": ["projects"], "entities": [{"name": "Milestone", "fields": [{"name": "project_id", "type": "uuid", "references": "projects.Project"}, {"name": "title", "type": "str", "max_length": 120}], "operations": ["create", "get", "list"]}]}]}), encoding="utf-8")
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract_path)))
        self.assertEqual(plan.state, "planned", (plan.blockers, plan.conflicts))
        self.assertTrue(plan.migration["required"])
        self.assertEqual(plan.migration["tables"], ["milestones"])
        self.assertTrue(any(r.startswith("row-level security: every new table is protected") for r in plan.risks), plan.risks)
        self.assertIn("tests/test_rls.py", plan.modify)
        for untouched in ("src/tenant_projects/core/db.py", "src/tenant_projects/core/security.py", "src/tenant_projects/core/authz.py"):
            self.assertNotIn(untouched, plan.modify)
        delta = next(c for p, c in plan.files.items() if p.startswith("migrations/versions/") and p != plan.migration["historical_files_kept"][0])
        self.assertIn('_protect("milestones")', delta)
        self.assertNotIn('_protect("projects")', delta)
        self.assertEqual(plan.evolved_spec.schema, "fastapi-architect/spec/1.2")

    def test_a_declared_but_unimplemented_strategy_is_named_in_the_plan_and_never_upgraded(self) -> None:
        project = self._copy(SNAPSHOT_ORDERS, "orders")
        contract_path = self.tmp / "evolve.json"
        contract_path.write_text(json.dumps({"schema": evolution.EVOLUTION_SCHEMA, "project": {"path": str(project)}, "add_modules": [{"name": "shipments", "depends_on": ["orders"], "entities": [{"name": "Shipment", "fields": [{"name": "order_id", "type": "uuid", "references": "orders.Order"}, {"name": "carrier", "type": "str", "max_length": 60}], "operations": ["create", "get", "list"]}]}]}), encoding="utf-8")
        plan = evolution.plan_evolution(evolution.load_evolution(str(contract_path)))
        self.assertEqual(plan.state, "planned", (plan.blockers, plan.conflicts))
        self.assertTrue(any("shared_schema_rls is declared in a 1.1 contract and stays unimplemented" in r for r in plan.risks), plan.risks)
        self.assertEqual(plan.evolved_spec.schema, "fastapi-architect/spec/1.1")
        self.assertFalse(plan.evolved_spec.rls)
        delta = next(c for p, c in plan.files.items() if p.startswith("migrations/versions/") and p not in plan.migration["historical_files_kept"])
        self.assertNotIn("ROW LEVEL SECURITY", delta)

    def test_manifests_record_the_implemented_security_model(self) -> None:
        def manifest(snapshot: Path) -> dict:
            return json.loads((snapshot / ".fastapi-architect" / "generation.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest(SNAPSHOT_TENANT_PROJECTS)["security"], {"authentication": "api_key", "identity": "per_caller", "authorization_model": "roles_and_ownership", "authorization_declared": None, "tenant_strategy_declared": "shared_schema_rls", "tenant_strategy_implemented": "shared_schema_rls", "tenant_binding": "api_key_registry", "row_level_security": True})
        orders = manifest(SNAPSHOT_ORDERS)["security"]
        self.assertEqual((orders["tenant_strategy_declared"], orders["tenant_strategy_implemented"], orders["row_level_security"], orders["authorization_declared"], orders["authorization_model"]), ("shared_schema_rls", "shared_schema_application_scoping", False, "resource", "none"))
        for snapshot in (SNAPSHOT_TENANT_PROJECTS, SNAPSHOT_TEAM_NOTES, SNAPSHOT_ORDERS):
            self.assertEqual(manifest(snapshot)["schema"], "fastapi-architect/generation/1.2")
            model = design.discover(str(snapshot))
            self.assertIsNotNone(model.baseline_spec)


class SecurityCliTests(TempDirCase):
    def test_validate_only_reports_the_isolation_actually_generated(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT_CLI), str(TENANT_PROJECTS), "--validate-only", "--format", "json"], capture_output=True, text=True, cwd=str(self.tmp), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["guarantees"]["multi_tenant"]["row_level_security"])
        self.assertEqual(report["guarantees"]["authorization_model"], "roles_and_ownership")
        proc = subprocess.run([sys.executable, str(ROOT_CLI), str(ORDERS), "--validate-only", "--format", "json"], capture_output=True, text=True, cwd=str(self.tmp), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["guarantees"]["multi_tenant"]["row_level_security"])


if __name__ == "__main__":
    unittest.main()
