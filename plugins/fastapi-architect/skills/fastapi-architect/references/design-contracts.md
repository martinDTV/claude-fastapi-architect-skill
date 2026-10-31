# Design contracts (v2.0): specification, recommendation, generation, verification, report

The design engine turns a **declarative architecture contract** into a reasoned structure recommendation, a
runnable FastAPI project and a verification report. It reuses the static auditor (v1.3–v1.9) to review what it
generated and the v1.9 trust model to run the generated tests; it never executes the contract, never overwrites
files without an explicit flag, and separates what the author stated from what the generator assumed.

```
python3 scripts/design_fastapi.py SPEC.json --validate-only
python3 scripts/design_fastapi.py SPEC.json --target DIR [--allow-existing] [--overwrite] [--dry-run] [--no-audit]
       [--authorize-execution --trust fixture|trusted --test-command "python3 -m pytest -q"]
       [--output report.json] [--format text|json|markdown] [--strict]
```

Exit codes: `0` completed; `1` with `--strict` when the audit of the generated project reports findings or the
tests did not pass; `2` invalid specification, refused generation or invalid arguments.

## 1. Specification (`fastapi-architect/spec/1.2`; `1.1` and `1.0` documents are still accepted)

Plain JSON, validated strictly like `.audit-fastapi.json`: unknown properties are errors (annotations prefixed
`x-` are allowed), identifiers are checked, enumerations are closed, and every problem is reported with its JSON
path. Validation collects all errors instead of stopping at the first one. Schema 1.1 (increment 2) adds
`references`/`on_delete` on uuid fields and `security.multi_tenant.binding`; a 1.0 document is a 1.1 document
without them. Schema 1.2 (increment 5) adds the security design contract of section 8: `security.oidc`, the
authorization object (`model`, `roles`, `operations`, `ownership`), `owned` entities, the tenant strategy
`shared_schema_application_scoping` (the default of 1.2 documents), the `principal_claim` binding,
`multi_tenant.rls` and `security.audit`. A 1.0/1.1 document keeps its meaning under the 1.2 engine: its
`authorization` string stays a declaration and a declared `shared_schema_rls` stays application scoping (section 6).

| Key | Content | Default when omitted |
|---|---|---|
| `schema` | `fastapi-architect/spec/1.1` or `fastapi-architect/spec/1.0` | required |
| `name` | service name (letters, digits, space, `.`, `_`, `-`) | required |
| `package` | python package name (lowercase identifier) | derived from `name` |
| `description` | free text | `<name> backend` |
| `scope` | `size` small/medium/large, `team_size`, `expected_rps`, `data_volume` | small, 2, 50, small |
| `functional.modules[]` | `name` (identifier, not reserved), `description`, `depends_on` (other modules, acyclic), `entities[]` | required, at least one |
| `entities[]` | `name` (CapitalizedWord), `fields[]`, `operations` ⊆ create/get/list/update/delete, `owned` (1.2: the row records the subject of the principal that created it in `owner_id`; needs an ownership model) | all operations, not owned |
| `fields[]` | `name` (identifier; `id`, `tenant_id`, `owner_id`, `created_at`, `updated_at` are generated and cannot be declared), `type` str/int/float/bool/uuid/datetime/date/decimal, `required`, `max_length` (str only), `min_value`/`max_value` (numeric only), `description`, `references` (`"module.Entity"` or `"Entity"`, uuid only: a foreign key in SQL persistence; the target must be in the same module or in a declared `depends_on`), `on_delete` restrict/cascade/set_null (`set_null` needs `required: false`) | required true, on_delete restrict |
| `nonfunctional` | `availability`, `latency_p95_ms`, `consistency` strong/eventual, `auditability`, `compliance[]` | 99.5%, 300, strong, false, [] |
| `persistence` | `kind` memory/postgresql, `migrations` (postgresql only) | memory, false |
| `security` | `authentication` none/api_key/oauth2_jwt/session; `oidc` (1.2, required with oauth2_jwt; section 8); `authorization`: the legacy string none/role/resource (a declaration, never generated) or the 1.2 object {`model` none/roles/ownership/roles_and_ownership, `roles[]`, `operations` {operation: roles}, `ownership` {`visibility` tenant/owner, `admin_roles[]`}}; `multi_tenant` {`enabled` (needs authentication), `strategy` shared_schema_application_scoping (1.2)/shared_schema_rls/schema_per_tenant/database_per_tenant, `tenant_param`, `binding` none/api_key_registry/principal_claim (how the authenticated principal is bound to the tenant it addresses; `api_key_registry` needs `authentication: api_key`, `principal_claim` needs `oauth2_jwt`), `rls` {`app_role`, `context_key`} (only with shared_schema_rls)}; `audit` {`authorization_decisions`} | none, none, disabled / shared_schema_application_scoping in 1.2 (shared_schema_rls in 1.0/1.1) / `tenant_id` / none; `<package>_app`, `app.tenant_id`; false |
| `integrations[]` | `name`, `kind` http/queue/smtp/storage/database, `direction` outbound/inbound, `criticality` low/medium/high, `module` | outbound, medium |
| `infrastructure` | `deployment` container/vm/serverless, `cache`, `broker`, `regions` | container, false, false, 1 |
| `architecture` | `style_preference` layered/modular_monolith/hexagonal/microservices, `independent_deployment` | none, false |
| `decisions[]` | `id`, `title`, `status` accepted/pending/rejected, `rationale` (required when accepted) | pending |
| `components[]` | documentation of named components with `depends_on` (identifiers checked) | [] |
| `testing` | `levels` ⊆ unit/integration/contract/end_to_end, `coverage_target` | unit+integration, 80 |
| `assumptions[]`, `open_questions[]` | author's own assumptions and open questions | [] |

**Provenance.** Every value gets a mark: `explicit` (written by the author) or `assumed` (filled with a default).
The report counts explicit values, lists the assumed paths, and lists the pending decisions (`decisions` with
`status: pending`, plus `open_questions`). **Digest**: sha256 of the canonical JSON of the document (sorted keys,
compact separators); the generation manifest and the report carry it, so a generated project can be traced to the
exact contract.

Cross-field rules enforced: migrations need postgresql; authorization and multi-tenancy need authentication;
`max_length` only on `str`, `min_value`/`max_value` only on numeric types and consistent; module dependencies
must exist and be acyclic; references name existing entities and follow the dependency direction; a tenant
binding needs multi-tenancy enabled and (for `api_key_registry`) API-key authentication; an accepted decision
must state its rationale; module and entity names are unique. Schema 1.2 adds the security rules of section 8:
`oauth2_jwt` needs `oidc` (https issuer and JWKS URL, asymmetric algorithms only), `principal_claim` needs
`oauth2_jwt`, roles and ownership need a caller identity (a shared API key has none: use `oauth2_jwt` or the
`api_key_registry` binding), per-operation roles and admin roles must be declared roles, an ownership model needs
at least one `owned` entity and vice versa, `shared_schema_rls` needs PostgreSQL and a binding, `rls` needs
`shared_schema_rls`, and 1.2-only keys are rejected in 1.0/1.1 documents with the schema they need.

## 2. Recommendation (advisor)

Deterministic, derived from the signals of the contract; never a default dogma (no DDD, CQRS, event sourcing,
microservices or hexagonal "by default"). Styles and their triggers:

| Style | Recommended when | Generated layout |
|---|---|---|
| `layered` | otherwise (one or two modules, small scope, small team) | `src/<pkg>/{api,schemas,services,repositories,models,dependencies}/<module>.py` |
| `modular_monolith` | three or more modules, or medium/large scope, or team of four or more | `src/<pkg>/modules/<module>/{router,schemas,service,repository,models,dependencies}.py`, import-linter contracts between modules |
| `hexagonal` | two or more outbound integrations, or a high-criticality integration, or contract tests with outbound integrations | the modular layout plus `ports.py` (Protocols) and `adapters/` in the integrating modules only |
| `microservices` | `architecture.independent_deployment` **and** (an accepted deployment decision or team of eight or more) **and** four or more modules | one modular monolith per bounded context as the first step (the generator does not split deployables) |

An explicit `style_preference` is honoured; when the signals point elsewhere the report records a warning with
the computed alternative. The recommendation carries: `rationale`, `rejected` (each alternative with its reason),
`assumptions` (assumed paths plus the author's own), `pending` (decisions, open questions and what the generator
deliberately did not take: Alembic environment, outbox/inbox for eventual consistency or a broker, audit trail,
authorization policy, tenant membership binding, OAuth2/session authentication, end-to-end tests), `signals`,
`ports_modules`, and persistence/security/testing notes.

## 3. Blueprint and generation

The blueprint is a pure function `(spec, recommendation) -> {relative path: content}`: sorted iteration, no
timestamps, no random values, so two runs produce identical bytes. Every project contains:

- `pyproject.toml` (fastapi, uvicorn, pydantic-settings; `sqlalchemy[asyncio]` + `asyncpg` for postgresql; pytest,
  httpx, anyio, ruff as dev dependencies; import-linter contracts for modular layouts; pytest `pythonpath = ["src"]`);
- `src/<pkg>/main.py` app factory with lifespan-owned resources, RFC 9457 problem details for domain errors, the
  `/health` route and every business router mounted under `/api/v1` (behind the API-key dependency when
  `authentication: api_key`, compared in constant time against a `SecretStr` setting with no default);
- `core/config.py` (pydantic-settings, env prefix `<PKG>_`, `current_settings` dependency so tests inject their
  own settings; no credential has a default; `api_keys` grants and `oidc_*` settings when declared), `core/errors.py`
  (403 `ForbiddenError`, 404, 409, 503 `UnavailableError`; on PostgreSQL an unreachable database is answered 503,
  never 401 or 404), `core/security.py` (api_key: one shared key; `api_key_registry`: `api_keys` maps each key to a
  grant with `tenant_id`, `subject` and `roles`, `require_tenant_member` resolves the tenant path parameter and
  refuses any other tenant with 403; `oauth2_jwt`: the OIDC validation of section 8), `core/authz.py` (the
  authorization policy of section 8, when `authorization.model` is not `none`), `core/db.py` (postgresql: async
  engine, declarative base with a naming convention, request-scoped `AsyncSession` that rolls back on errors; the
  **service** flushes and commits once per use case and turns an `IntegrityError` into a 409 after rolling back;
  under `shared_schema_rls` also `get_tenant_session`, which sets the tenant context of the authenticated principal
  for the current transaction only);
- for postgresql: `alembic.ini`, `migrations/env.py` (async, URL from `<PKG>_DATABASE_URL`, or
  `<PKG>_MIGRATIONS_DATABASE_URL` first under `shared_schema_rls`, every model registered), `migrations/script.py.mako`,
  and `migrations/versions/<rev>_initial_schema.py` generated from the contract (tables in dependency order, explicit
  constraint names, indexes on tenant, owner and foreign-key columns, revision id derived from the contract digest;
  under `shared_schema_rls` every tenant table is protected in the revision that creates it: section 8); `create_all`
  is never called;
- per module: schemas (`extra="forbid"`, constraints from the fields), models (dataclasses in memory, SQLAlchemy 2
  mapped classes for postgresql with `tenant_id`, `UniqueConstraint(tenant_id, id)` and `ForeignKeyConstraint`s from
  the declared references, composite over the tenant id between tenant tables), repository Protocol plus the
  chosen implementation (tenant-scoped on every call, no flush: the unit of work belongs to the service), service
  with the declared use cases, dependencies (composition), router with return annotations and status codes (the
  tenant comes from the bound principal, never from a bare path parameter); **only the declared operations** are
  generated;
- `tests/conftest.py` (one app per test with fresh in-memory state, recording adapters for ports; for postgresql
  the suite needs `DATABASE_URL` set to an **empty, disposable** database: it refuses a non-empty one, migrates,
  truncates after every test and downgrades at the end) and `tests/test_<module>.py` through the HTTP boundary:
  201 on create, 422 on invalid payload and on unknown properties, 200/404 on get with
  `application/problem+json`, 422 on a malformed id, pagination with limits, update, delete, 401 without the API
  key or the bearer token, the port notification when a port exists, 409 with no row persisted when a reference
  points at a missing row, and for multi-tenant contracts 403 for another tenant's path, empty listings across
  tenants, 404 for another tenant's id presented under the caller's tenant (the row stays untouched) and 409 when
  a reference points at another tenant's row; with a policy, the role each operation needs (`limited` gets 403),
  ownership across members and admins and the client's inability to set `owner_id`; `tests/test_auth.py` (OIDC)
  and `tests/test_rls.py` (row-level security) as in section 8; `tests/test_migrations.py` asserts one head, an
  empty `compare_metadata` (schema drift is detected), the constraints by name and repeatable up/down. The test
  identities are fixed: `alice` (tenant A, every role), `bob` (tenant A, member roles), `admin` and `limited`
  (tenant A, when the policy declares admin roles or a role model), `carol` (tenant B);
- `.audit-fastapi.json` declaring the design for the auditor: `public_routes` (`/health` and `/api/v1/*` when
  there is no authentication, `/health` otherwise; full paths, since the auditor composes `include_router`
  prefixes), `tenant_params`, `contracts.forbidden_imports` between modules that do not declare a dependency,
  `contracts.commit_layers: ["service"]` for postgresql, `contracts.route_auth_required` when authenticated,
  `contracts.mutation_authorization_required` when a policy is declared (a generated mutation that loses its policy
  dependency is reported through this declaration, not through a naming guess);
  `.env.example` with placeholders only, `.gitignore`, a README with the rationale, the migration commands, the
  tenant model and the pending decisions. The generated code passes its own ruff configuration (E, F, I, B, UP,
  ASYNC at 120 columns).

The generator **refuses** contracts whose `authentication` is `session`, `oauth2_jwt` contracts of schema 1.0/1.1
(no `oidc` block: a declaration without the provider configuration), and multi-tenant contracts whose `binding`
is `none`: the contract accepts them, the generator does not emit a non-functional stub, and a route that accepts
any `tenant_id` from any authenticated caller is not authorization. The refusal is reported (`generation_refused`)
and `--validate-only` still produces the recommendation and the guarantees block.

**Safety rules** (each enforced and tested):

| Situation | Behaviour |
|---|---|
| target does not exist | created |
| target exists and is not empty, no flag | refused, nothing written |
| `--allow-existing` | missing files are added; identical files are left untouched; differing files are **conflicts** |
| any conflict without `--overwrite` | nothing is written, the conflicts are listed with both digests, exit 2 |
| `--overwrite` | only differing files are replaced; nothing is ever deleted |
| `--dry-run` | the plan is reported, the target is not touched |
| symlinked target, a file as target, a directory where a file goes | refused |
| path outside the target (`..`, absolute, backslash) | refused before anything is written |

Files are written atomically (temporary file + rename). `.fastapi-architect/generation.json`
(`fastapi-architect/generation/1.2`; 1.0 and 1.1 are still read) records the tool version, the spec name and
digest, the contract document, the style, the package, the sha256 of every generated file, the migration chain
(each revision with `row_level_security`), the operation history and a `security` record (authentication,
identity, authorization model and legacy declaration, tenant strategy declared and implemented, binding,
`row_level_security`), without timestamps, so re-running the same contract changes nothing, `verify_manifest`
can tell `identical`, `modified` and `missing` files apart, and a later evolution can never mistake a declared
strategy for an implemented one.

## 4. Verification

- **Audit**: `audit_project` over the generated target (static, never executes it). The generated projects audit
  clean: router-level dependencies are propagated through `include_router` and route paths are composed through
  the mount chain (`effective_paths`, `path_resolution` in every route contract; see the release notes), the
  declared public routes and tenant parameters are honoured, and with `api_key_registry` the membership
  dependency is visible on every tenant route (`authorization_visible`).
- **Tests**: only with `--authorize-execution` and a trust profile accepted by an executor of the v1.9 trust
  model. The local executor provides process hygiene (no shell, allow-listed programs, fresh HOME, reduced
  environment, closed stdin, timeouts, CPU/file-size limits), not a sandbox; it refuses `untrusted`. The run is
  bound to a runner adapter (`pytest-junit`, `unittest-json`) that produces structured evidence and happens in a
  **copy** of the generated project (`Workspace.create`), so the target is never touched by its own tests or by
  the tooling they pull in; the copy's Python digest is compared before and after and reported under
  `verification.tests.workspace`. Pass `--env-passthrough DATABASE_URL,UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR` to run a
  PostgreSQL project with `uv run` (see `docs/guides/generated-postgresql-projects.md`). Without authorization the
  report says `tests: not executed` and why.

## 5. Report (`architecture_report/1.0`)

```json
{
  "schema": "architecture_report/1.0",
  "tool": {"name": "fastapi-architect design", "version": "2.0.0"},
  "spec": {"name", "schema", "digest", "source", "package", "modules", "entities", "persistence", "authentication", "multi_tenant", "explicit_paths", "assumed_paths", "document"},
  "recommendation": {"style", "rationale", "rejected", "assumptions", "pending", "signals", "ports_modules", "warnings", "persistence_notes", "security_notes", "testing_notes"},
  "guarantees": {"multi_tenant": {"requested", "strategy_declared", "strategy_implemented", "principal_tenant_binding", "storage_isolation", "row_level_security", "rls": {"app_role", "context_key", "context_scope", "context_set_by", "context_trust", "role_privileges", "owner"} | null, "schema_or_database_per_tenant": false}, "authentication", "identity": {"principal", "subject_source", "roles_source", "tenant_source"}, "resource_authorization", "authorization_model", "audit_logging", "persistence": {"kind", "migrations", "create_all_used": false, "constraint_violation_on_commit", "infrastructure_failure"}, "execution", "not_claimed"},
  "layout": ["tree lines"], "files": ["relative paths"], "blueprint_notes": [],
  "generation": {"target", "dry_run", "written", "unchanged", "conflicts", "manifest"} | null,
  "generation_refused": "reason" | null,
  "verification": {"audit": {"schema_version", "files_scanned", "findings_total", "by_severity", "findings", "contracts", "partial_analysis"}, "tests": {"ran", "executor", "run", "workspace": {"copied", "python_files_digested", "python_files_changed_by_the_run"}, "denied_reason", "denied_code"}} | null,
  "authorization": {"execution", "trust", "scope", "declared_by", "technical_guarantee": false},
  "next_steps": [], "note": "..."
}
```

`--format text` prints a compact summary, `--format markdown` the full report (recommendation, rejected
alternatives, assumptions, pending decisions, layout, generation, audit, tests). JSON is sorted and stable.

## 6. Multi-tenancy: what is guaranteed and what is not

The contract keeps six things apart, and the report's `guarantees` block states each one:

| Aspect | Contract | Generated (increments 2 and 5) |
|---|---|---|
| Multi-tenant design requested | `security.multi_tenant.enabled` | `tenant_id` on every entity, tenant-scoped routes and repositories |
| Isolation strategy selected | `strategy` | 1.0/1.1 documents, and 1.2 documents with `shared_schema_application_scoping`: tenant filter on every query, composite foreign keys on PostgreSQL, `row_level_security: false`; a 1.0/1.1 `shared_schema_rls` is **declared, not implemented** (pending decision). 1.2 documents with `shared_schema_rls` on PostgreSQL: row-level security generated and tested (section 8), `row_level_security: true`. Schema or database per tenant: recorded, not generated |
| Authenticated identity | `authentication` | `api_key` (shared key, no caller identity), `api_key_registry` (grant per key: tenant, subject, roles) or `oauth2_jwt` with `oidc` (verified token claims); sessions are refused |
| Principal-tenant binding | `binding` | `api_key_registry`: the tenant bound to the key; `principal_claim`: the verified tenant claim of the token; both compared with the tenant path parameter by `require_tenant_member` (403 otherwise); `none`: generation refused |
| Resource authorization | `authorization` | legacy string: not generated, pending decision; 1.2 object: per-operation roles in a route dependency and ownership in the service and the repository query (section 8), reported as `resource_authorization: <model>: ...` |
| Storage isolation | `strategy` | application scoping plus composite `(tenant_id, id)` foreign keys; under `shared_schema_rls` also ENABLE + FORCE row-level security on every tenant table, an application role without BYPASSRLS and a transaction-local tenant context |

Negative controls in the suite: a contract with `binding: none` validates, is refused for generation and never
gets `principal_tenant_binding: enforced`; a declared `shared_schema_rls` in a 1.0/1.1 document is never reported
as implemented and an evolution of such a project names it in its risks without upgrading it; a 1.2 contract that
asks for RLS on in-memory persistence or without a binding is rejected; the generated tests prove the 403, the
empty cross-tenant listings, the 404 on a foreign id and the 409 on cross-tenant references.

## 7. Limits

- Generated: layered, modular monolith and hexagonal layouts; in-memory and PostgreSQL persistence with an Alembic
  environment and the initial migration; API-key authentication (shared key or per-tenant grants) and OIDC bearer
  tokens; tenant scoping with the registry or the claim binding; per-operation roles and ownership; PostgreSQL
  row-level security as an opt-in strategy; HTTP and recording adapters for ports.
- Not generated: session authentication, refresh tokens or token issuance, public resources inside an
  authenticated API, cross-tenant administration, schema or database per tenant, outbox/inbox, workers, Docker/CI
  files (see `assets/`), end-to-end tests, service splitting for `microservices`, later migrations
  (`alembic revision --autogenerate` works on the generated metadata), `uv.lock` (resolution needs the index;
  version ranges are explicit).
- Verified against PostgreSQL: `orders-platform` and `tenant-projects` through the opt-in tests (`FA_DESIGN_PG=1`)
  on a throwaway cluster; the evidence is in `plugins/fastapi-architect/examples/generated/VERIFICATION.md`. Other
  PostgreSQL contracts are generated and linted but their suites are not executed by the plugin's tests.

## 8. Identity, authorization and tenant isolation (schema 1.2, increment 5)

The security block separates five things that are often conflated, and the generator builds each one only when
the contract makes it buildable; the report's `guarantees` block repeats what was built.

**Authentication: who is calling.** `api_key` with a shared key authenticates the deployment, not a caller: no
subject, no roles, no policy. With the `api_key_registry` binding every key is a grant `{"tenant_id", "subject",
"roles"}` (`<PKG>_API_KEYS`; a bare tenant id is still read as a grant without subject or roles, the subject then
being a digest of the key). `oauth2_jwt` needs `security.oidc`: `issuer` (https), `audience`, `algorithms`
(asymmetric only: RS/PS/ES/EdDSA; `HS*` and `none` are rejected by the contract), `jwks_url` (https; default
`<issuer>/.well-known/jwks.json`), `leeway_s` (0..300), `jwks_cache_s` (30..86400) and `claims` (`subject`, `tenant`,
`roles`; providers may namespace them as URLs). The generated `core/security.py` accepts a token only when its
header algorithm is in the contract's list (checked before any key lookup), the signature verifies against the
JWKS key named by `kid`, `iss`, `aud`, `exp`, `nbf` and `iat` hold, and the required claims are present; the principal
is built from verified claims only. The JWKS client (`JwksSource`): https only, certificate and hostname verification
always on and extensible with a private CA through `oidc_ca_bundle` (never disabled), the response capped at 256 KiB,
the key set cached for `jwks_cache_s`, one fetch attempt per 30 s whatever the reason (expiry, unknown `kid`, failure),
the last keys served for at most `oidc_jwks_stale_max_s` when a refresh fails and nothing served without a usable set,
a set without any asymmetric key treated as unavailable, a fetch timeout of `oidc_fetch_timeout_s`, and an injectable
clock. A static JWKS (`<PKG>_OIDC_JWKS_JSON`) is accepted only when `<PKG>_ENVIRONMENT=test`; any provider failure is
503 (`JwksUnavailable`, logged without the token) and never 401. The generated `tests/test_jwks.py` proves the client
against an HTTPS provider started on 127.0.0.1 with a certificate authority created for the session: fetch and
signature, cache, rotation after the interval, no fetch per unknown `kid`, a key replaced under the same `kid` rejected
until expiry, a removed key rejected, malformed JSON, invalid and symmetric-only sets, HTTP errors, a slow provider,
TLS failures with the system store and with another CA, outage without cache and recovery after the interval, stale
service within the window only, eight concurrent cold starts fetching once. The generated `tests/test_auth.py`
holds the negative controls: altered claims with the original signature, expired, not yet valid, wrong audience
or issuer, HS256 with the public key as secret, `alg: none`, unknown or missing `kid`, another key under the same
`kid`, malformed tokens, missing required claims, a malformed roles claim, a token without a usable tenant claim,
a member of another tenant (403, not 401), the static JWKS outside the test environment, an http JWKS URL and the
unreachable provider. Not generated: refresh tokens, sessions, token issuance, an authorization server.

**Principal-tenant binding: which tenant the caller may address.** `api_key_registry` (the grant's tenant) or
`principal_claim` (the verified tenant claim, which must be a uuid; a token without it is 401). In both cases
`require_tenant_member` compares the bound tenant with the tenant path parameter and answers 403 otherwise, and
every repository call is scoped by the bound tenant: possessing a `tenant_id` never authorizes anything.

**Per-operation authorization: may this caller do this at all.** `authorization.model` `roles` or
`roles_and_ownership` with `roles[]` and `operations` (operation -> roles; an operation absent from the map is
open to every authenticated member of the tenant). `core/authz.py` exposes `CanCreate`, `CanGet`, `CanList`,
`CanUpdate`, `CanDelete`: route dependencies that admit the principal for that operation or answer 403 before the
use case runs. Admin roles are ordinary declared roles listed in `ownership.admin_roles`.

**Per-resource authorization: may this caller act on this row.** `owned: true` entities record `owner_id` (the
principal's subject) at creation; the client cannot set it (422). `ownership.visibility` `tenant`: every member
reads, only the owner or an admin updates or deletes (403 otherwise). `owner`: only the owner or an admin reads,
updates or deletes; to anyone else the row does not exist (404), because the repository query itself is
owner-scoped (`owner=read_scope(principal)`). The service enforces it (`ensure_owner`) on the row it loaded; the
router only carries the principal. 404 is also the answer for a foreign tenant's id presented under the caller's
tenant (tenant-scoped query); 403 is reserved for "visible, but not yours to change" and for the wrong tenant path;
503 is an infrastructure failure. `security.audit.authorization_decisions: true` logs every denial (subject,
tenant, operation, resource type, reason; never a credential) through `logging.getLogger("<pkg>.authz")`.

**Storage isolation: what the database itself enforces.** `shared_schema_application_scoping` (the 1.2 default)
is the tenant filter on every query plus composite foreign keys. `shared_schema_rls` (PostgreSQL only, binding
required) adds, in the migration that creates each tenant table: `GRANT SELECT, INSERT, UPDATE, DELETE` to the
application role, `ENABLE` and `FORCE ROW LEVEL SECURITY`, and one policy `<table>_tenant_isolation` whose `USING`
and `WITH CHECK` compare the tenant column with `NULLIF(current_setting('<context_key>', true), '')::uuid` (no
context, or an empty one: no row visible or writable; an invalid one fails the query). The application role
(`<PKG>_APP_ROLE`, default `rls.app_role`) is provisioning, not a migration: the migration verifies on the live
connection that it exists and is neither superuser nor BYPASSRLS, and refuses otherwise (transactional DDL leaves
no table behind). The application connects as that role (`<PKG>_DATABASE_URL`); migrations and the test
truncation run as the owner (`<PKG>_MIGRATIONS_DATABASE_URL`). `get_tenant_session` sets the context with
`set_config('<context_key>', <principal tenant>, true)` at the start of the request transaction, never with a
session-level `SET`; commit, rollback and the return of a pooled connection clear it. The generated
`tests/test_rls.py` proves it through the application role with raw SQL: policies enabled and forced on every
tenant table, the role restricted, no rows and no inserts without context, only the caller's tenant with context,
another tenant's rows neither inserted, moved, updated nor deleted, the context gone after commit and rollback and
on the next checkout of the same pooled connection, a rolled-back insert leaving no row, invalid and empty
contexts failing closed, the role unable to disable or un-force RLS, drop the policy, truncate or grant itself
BYPASSRLS, 24 concurrent requests of two tenants through the ASGI app never mixing rows, and an unreachable
database answered 503. An evolution that adds a module to such a project protects the new table in its additive
revision and regenerates `tests/test_rls.py`; a 1.0/1.1 project that declared `shared_schema_rls` keeps
application scoping and the plan says so (moving to RLS is a reviewed design change with new tests, never an
evolution, and `security`/`persistence` are not evolution operations).

**Threat model of the row-level security (what the mechanism does and does not stop).** *Who sets the context:*
`get_tenant_session`, from the authenticated principal's tenant, with `set_config(..., true)` before the first
statement of the request transaction; the request never supplies it. *Who can change it:* any statement executed on
that connection in that transaction as the application role: the application's own code, a raw `text()` statement, or
an injection that reaches statement level; a bound parameter cannot (the generated `tests/test_rls.py` demonstrates
both: a `set_config` statement switches tenant, the same text as a parameter does not). *What the role may do:*
`SELECT`, `INSERT`, `UPDATE`, `DELETE` on the tenant tables and `USAGE` on the schema; no DDL, no `TRUNCATE`, no
`BYPASSRLS`, no `CREATEROLE`, no `alembic_version`, no `pg_authid` (demonstrated). *SQL injection:* the generated
code only issues parameterised statements; an injection able to run statements as the application role defeats the
tenant boundary for that session and is outside what RLS promises. *Arbitrary SQL as the role:* same limit; the
role still cannot lift the policies, create objects or read secrets. *Transactions and pooling:* the context ends
with commit or rollback; the service commits once at the end of the use case and issues no statement afterwards;
SQLAlchemy resets the connection on return, `pool_pre_ping` discards dead ones, and the next checkout starts without
context (fail closed). *FORCE and composite keys:* `FORCE` subjects the owner to the policies unless it is a superuser
or `BYPASSRLS`, so an owner that runs data migrations needs a context or `BYPASSRLS`; composite `(tenant_id, id)`
foreign keys stop cross-tenant references even for roles that bypass RLS. *A compromised principal:* valid
credentials of tenant A reach only tenant A and only the operations its roles allow; roles come from the grant or
the token, never from the request. *Not provided, documented as future architecture:* a context the application
cannot rewrite (`SECURITY DEFINER` wrappers over a signed context token), one database role per tenant with `SET
ROLE` at the pool boundary, a schema or database per tenant.

**Unsupported or impossible configurations (rejected by the contract or refused by the generator):** `oauth2_jwt`
without `oidc` (1.0/1.1), symmetric or unsigned algorithms, http issuer or JWKS URL, `principal_claim` with API
keys, `api_key_registry` with OIDC, roles or ownership on a shared API key, owned entities without an ownership
model or an ownership model without owned entities, undeclared roles in `operations` or `admin_roles`,
`shared_schema_rls` on in-memory persistence or without a binding, `rls` settings with another strategy, `audit`
without authentication, `session` authentication, multi-tenancy without a binding, schema or database per tenant,
public resources inside an authenticated API, cross-tenant administration.

**Verification procedures.** `tests/test_design_v20_security.py` validates the contract rules, the generated code,
the guarantees (frozen per contract under `tests/fixtures/design20/guarantees`: a change in a claim is a reviewed
fixture edit), the auditor on the generated projects (zero findings above `info`, every protected route with a
visible authorization kind, and two negative controls: a route stripped of its policy and principal is reported
through `CONTRACT_MUTATION_AUTHZ_REQUIRED`, a session-level `SET` through `SEC_RLS_SET_NOT_LOCAL`), the evolution
rules, and runs the generated suites of `team-notes` (OIDC + `principal_claim` + `roles_and_ownership`, in memory,
including `tests/test_jwks.py` against the local HTTPS provider) and of the single-tenant owner-scoped `journal`
fixture through the trust model when PyJWT and cryptography are importable. `tests/test_audit_v20_named_authz.py`
holds the auditor's negative controls for policy-like names (seven dependencies that decide nothing, six that do:
`references/audit-engine.md`). `tests/test_design_v20_postgresql.py` (`FA_DESIGN_PG=1`) runs the generated suite of `tenant-projects`
on a throwaway PostgreSQL 16 as the restricted role `fa_app`, checks that a BYPASSRLS role and a missing role make
the migration refuse without leaving tables behind, and evolves the project with a new module whose table is
protected. Evidence: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.
