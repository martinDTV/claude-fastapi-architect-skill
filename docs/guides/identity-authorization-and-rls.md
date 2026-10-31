# Identity, authorization and tenant isolation in generated projects

A walkthrough of what a schema 1.2 contract can ask for, what the generator builds for each request, how to run the
generated proofs, and where the limits are. The contract reference is
`plugins/fastapi-architect/skills/fastapi-architect/references/design-contracts.md` (section 8); the two committed
examples are `team-notes` (OIDC, tenant from a verified claim, roles and ownership, in memory) and
`tenant-projects` (API-key grants, roles and ownership, PostgreSQL row-level security).

## 1. Five layers, five decisions

| Layer | Contract key | What it answers |
|---|---|---|
| Authentication | `security.authentication` (+ `security.oidc`) | who is calling |
| Principal-tenant binding | `security.multi_tenant.binding` | which tenant the caller may address |
| Per-operation authorization | `security.authorization.model`, `roles`, `operations` | may this caller do this at all |
| Per-resource authorization | `entities[].owned`, `security.authorization.ownership` | may this caller act on this row |
| Storage isolation | `security.multi_tenant.strategy` (+ `rls`) | what the database enforces by itself |

Each layer is built only when the contract makes it buildable, and the report's `guarantees` block lists what was
built. Possessing a `tenant_id` never authorizes anything: the tenant comes from the grant or from a verified claim
and is compared with the path parameter; the routes never read it from the request as authority.

## 2. The contract

```json
"security": {
  "authentication": "oauth2_jwt",
  "oidc": {"issuer": "https://login.example.com/", "audience": "team-notes-api", "algorithms": ["RS256"],
           "claims": {"subject": "sub", "tenant": "workspace_id", "roles": "roles"}},
  "authorization": {"model": "roles_and_ownership", "roles": ["admin", "editor", "viewer"],
                    "operations": {"create": ["admin", "editor"], "update": ["admin", "editor"], "delete": ["admin", "editor"]},
                    "ownership": {"visibility": "tenant", "admin_roles": ["admin"]}},
  "multi_tenant": {"enabled": true, "strategy": "shared_schema_application_scoping", "tenant_param": "workspace_id", "binding": "principal_claim"},
  "audit": {"authorization_decisions": true}
}
```

and on the entity that has an owner: `{"name": "Note", "owned": true, "fields": [...]}`. For API keys instead of
OIDC: `"authentication": "api_key"` with `"binding": "api_key_registry"`; the keys become grants
(`PKG_API_KEYS={"<key>": {"tenant_id": "...", "subject": "ops", "roles": ["admin"]}}`). For row-level security:
`"persistence": {"kind": "postgresql"}` and `"strategy": "shared_schema_rls"` (optionally
`"rls": {"app_role": "tenant_projects_app", "context_key": "app.tenant_id"}`).

Validate first and read the guarantees back:

```
python3 scripts/design_fastapi.py plugins/fastapi-architect/examples/specs/team-notes.json --validate-only --format markdown
```

Impossible combinations are errors with their JSON path (RLS on in-memory persistence, `principal_claim` with API
keys, roles on a shared API key, `HS256`, an http issuer, an owned entity without an ownership model, ...); the
full list is in the reference. A 1.0/1.1 contract keeps its old meaning: its `authorization` string stays a
pending declaration and a declared `shared_schema_rls` stays application scoping, and the report says so.

## 3. What is generated

- `core/security.py`: for OIDC, a `JwksSource` (https only with TLS verification always on, a private CA added
  through `oidc_ca_bundle`, response capped, one fetch attempt per 30 s, a bounded stale window when a refresh fails,
  nothing served without a usable key set, static JWKS only in the test environment; a provider failure is
  `JwksUnavailable` and 503), `verify_token` (algorithm allow-list checked before any key lookup, signature,
  issuer, audience, `exp`/`nbf`/`iat`, required claims) and `require_bearer_token`, which builds the `Principal`
  (subject, tenant, roles) from verified claims only; for API-key grants, `require_api_key` with a constant-time
  comparison over every key. With a tenant binding, `require_tenant_member` compares the bound tenant with the path
  parameter (403).
- `core/authz.py`: `OPERATION_ROLES`, `ADMIN_ROLES`, `OWNER_VISIBILITY`, the `CanCreate`/`CanGet`/`CanList`/
  `CanUpdate`/`CanDelete` dependencies (403 before the use case runs), `read_scope` (the owner a repository read
  must filter by) and `ensure_owner` (403 on a loaded row that is not the caller's). Denials are logged when
  `audit.authorization_decisions` is true, never with a credential.
- Services take the principal, set `owner_id` from it at creation and check ownership on the row they loaded;
  repositories take an optional `owner` and filter in the query, so a row outside the caller's visibility is a 404
  and never a 403. Routers only carry the principal.
- `core/errors.py`: 403 `ForbiddenError`, 404, 409, 503 `UnavailableError`; on PostgreSQL an unreachable database is
  503, never 401 or 404.
- Under `shared_schema_rls`: the migration protects every tenant table in the revision that creates it (grants to
  the application role, `ENABLE` + `FORCE ROW LEVEL SECURITY`, one `USING`/`WITH CHECK` policy on
  `NULLIF(current_setting('<context_key>', true), '')::uuid`), after verifying on the live connection that the role
  exists and is neither superuser nor `BYPASSRLS`; `core/db.py` adds `get_tenant_session`, which sets the context
  with `set_config(..., true)` for the request transaction only; `migrations/env.py` reads
  `<PKG>_MIGRATIONS_DATABASE_URL` first.
- Tests: `tests/test_auth.py` (OIDC negative controls), ownership and role tests inside each module's suite with
  fixed identities (`alice`, `bob`, `admin`, `limited`, `carol`), and `tests/test_rls.py` (the row-level-security
  proofs through the application role).

## 4. Running the proofs

In-memory projects run everywhere the generated dependencies install:

```
python3 scripts/design_fastapi.py plugins/fastapi-architect/examples/specs/team-notes.json --target /tmp/team-notes \
    --authorize-execution --trust fixture --test-command "python3 -m pytest -q"
```

A PostgreSQL project with row-level security needs two roles and two URLs. Create the application role once
(roles are credentials, the migration never creates one):

```
CREATE ROLE tenant_projects_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
```

then, inside the generated project, with an **empty, disposable** database:

```
export DATABASE_URL=postgresql+asyncpg://tenant_projects_app:...@localhost:5432/tp_test        # the application
export TENANT_PROJECTS_MIGRATIONS_DATABASE_URL=postgresql+asyncpg://owner:...@localhost:5432/tp_test
export TENANT_PROJECTS_APP_ROLE=tenant_projects_app
uv run pytest -q
```

The suite migrates and truncates as the owner, runs the API as the application role, and `tests/test_rls.py`
demonstrates with raw SQL through that role: policies enabled and forced on every tenant table; the role is not a
superuser and cannot bypass RLS; without context no row is visible and no insert is accepted; with the context of
tenant A only A's rows are visible and B's rows cannot be inserted, moved, updated or deleted; the context does not
survive commit, rollback or the next checkout of the same pooled connection; a rolled-back insert leaves no row;
an invalid or empty context fails closed; the role cannot disable or un-force RLS, drop the policy, truncate or
grant itself `BYPASSRLS`; 24 concurrent requests of two tenants never mix rows; an unreachable database is 503.

`tests/test_rls.py` also shows the boundary: a `set_config` statement run as the application role switches the
tenant of that transaction (the policies read a setting that role may write), while the same text sent as a bound
parameter changes nothing; and it lists what the role cannot do (read `pg_authid` or `alembic_version`, create
tables or policies, take ownership). Section 8 states the threat model.

For OIDC projects, `tests/test_jwks.py` starts an HTTPS identity provider on 127.0.0.1 with a certificate authority
created for the session (trusted only through `oidc_ca_bundle`) and proves the client: fetch and signature, cache,
key rotation, no fetch per unknown `kid`, a key replaced under the same `kid`, a removed key, malformed and invalid
documents, HTTP errors, a slow provider, TLS failures, outage and recovery, the stale window, concurrent cold starts.
Every provider failure is 503 without a challenge; every token failure is 401 with one.

The plugin runs all of this itself on a throwaway PostgreSQL 16 when asked:

```
FA_DESIGN_PG=1 python3 -m unittest tests.test_design_v20_postgresql.RowLevelSecurityBackendTests -v
```

including two negative controls (a `BYPASSRLS` role and a missing role make the migration refuse and leave no
table behind) and an evolution that adds a module whose table is protected in the additive revision. The run that
accompanies the increment is recorded in `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.

## 5. Reading the status codes

| Answer | Meaning |
|---|---|
| 401 (`WWW-Authenticate: Bearer` for OIDC) | no credential, or a credential the service could not verify |
| 403 | authenticated, but the wrong tenant path, an operation the roles do not allow, or a visible row that is not the caller's to change |
| 404 | the row does not exist for this caller: unknown id, another tenant's id, or (visibility `owner`) another member's row |
| 422 | malformed id or payload (including an attempt to set `owner_id`) |
| 503 | infrastructure: the identity provider's keys or the database could not be reached |

## 6. Evolving safely

`security` and `persistence` are not evolution operations: an evolution contract that names them is rejected. An
evolution that adds a module to an RLS project protects the new table in its additive revision and regenerates
`tests/test_rls.py` (`plugins/fastapi-architect/examples/evolutions/tenant-projects-add-milestones.json`); the plan's
risks say so. A project generated from a 1.0/1.1 contract that declared `shared_schema_rls` keeps application
scoping, the plan names it, and nothing is upgraded silently: moving to RLS is a new, reviewed design with its own
tests. The manifest (`fastapi-architect/generation/1.2`) records the implemented security model so this can never
be confused later.

## 7. Limits

No session authentication, no OAuth2 flows (code, PKCE, device), consent, refresh tokens or token issuance (only
externally issued tokens are validated); no public resources inside an authenticated API; no cross-tenant
administration; no schema or database per tenant; the OIDC provider is trusted for the claims it signs (the service
validates, it does not manage identities) and the JWKS client is verified against a local HTTPS provider, not
against a real one; the static JWKS used by the generated tests is refused outside the test environment; the
throwaway-cluster verification uses the bootstrap superuser as the migrations owner (FORCE is verified through the
catalog flags and through the restricted role, not through a non-superuser owner); the local executor is process
hygiene, not a sandbox; the auditor counts a policy only where its code shows the decision, never by the name of a
dependency, and says so in `authorization_named_only`.

## 8. Threat model of the row-level security

| Question | Answer |
|---|---|
| Who sets the tenant context | `get_tenant_session`, from the authenticated principal's tenant, with `set_config(..., true)` before the first statement of the request transaction; never the request |
| Who can change it | any statement run on that connection in that transaction as the application role: application code, a raw `text()`, or an injection that reaches statement level. A bound parameter cannot (demonstrated both ways in `tests/test_rls.py`) |
| What the application role may do | `SELECT`, `INSERT`, `UPDATE`, `DELETE` on the tenant tables, `USAGE` on the schema. Not: DDL, `TRUNCATE`, `BYPASSRLS`, `CREATEROLE`, `alembic_version`, `pg_authid`, policies, ownership (demonstrated) |
| SQL injection | the generated code only issues parameterised statements; an injection able to execute statements as the role defeats the tenant boundary for that session. RLS protects against missing filters and wrong ids, not against arbitrary SQL |
| Arbitrary SQL as the role | same boundary; the role still cannot lift the policies, create objects or read secrets |
| Transactions and pooling | the context ends with commit or rollback; the service commits once at the end of the use case; SQLAlchemy resets connections on return and `pool_pre_ping` discards dead ones; the next checkout starts without context (fail closed) |
| `FORCE` and composite keys | `FORCE` subjects the owner to the policies unless it is a superuser or `BYPASSRLS`; composite `(tenant_id, id)` foreign keys stop cross-tenant references even for roles that bypass RLS |
| A compromised principal | valid credentials of tenant A reach tenant A only, with the operations its roles allow; roles come from the grant or the token, never from the request |
| Future options, not generated | a context the application cannot rewrite (`SECURITY DEFINER` wrappers over a signed context token), one database role per tenant with `SET ROLE` at the pool boundary, a schema or database per tenant |

The report's `guarantees.multi_tenant.rls` block repeats these answers (`context_set_by`, `context_trust`,
`role_privileges`, `owner`) and `not_claimed` lists the isolation against SQL injection as not provided.
