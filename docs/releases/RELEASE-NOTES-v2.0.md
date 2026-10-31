# FastAPI Architect v2.0.0 — Architecture & Engineering Foundations

**Status: increments 1 to 5 implemented and validated locally; pending human review; not published.**

v2.0 turns FastAPI Architect from an auditor and remediator into a system that can also **design and build**
professional backends, reusing the audit engine (v1.3–v1.8) and the remediation trust model (v1.9) instead of
rewriting them. This first increment delivers the declarative architecture contract, the reasoned structure
advisor, a safe and deterministic generator, verification through the existing engines, the report contract, the
CLI, two example contracts, a committed generated backend, and the tests, docs and compatibility controls around
them. Nothing cloud-native or distributed is started.

## What ships

### Architecture contract (`fastapi-architect/spec/1.0`)

- `fastapi_design/spec.py`: strict validation of a JSON contract with functional modules/entities/fields/operations,
  scope, non-functional requirements, persistence, security (authentication, authorization, multi-tenancy),
  integrations, infrastructure, architecture preferences, decisions, components, testing, assumptions and open
  questions. Unknown properties (except `x-` annotations), bad identifiers, reserved names, duplicate names,
  dependency cycles, inconsistent choices (migrations without PostgreSQL, authorization or tenancy without
  authentication, `max_length` on non-strings, accepted decisions without rationale) and wrong types are all
  reported at once, each with its JSON path.
- **Provenance** per value (`explicit` / `assumed`), pending decisions and open questions, and a canonical sha256
  digest of the contract.

### Advisor (`fastapi_design/advisor.py`)

- Deterministic recommendation among `layered`, `modular_monolith`, `hexagonal` and `microservices` from the
  contract's signals (modules, scope, team, outbound and high-criticality integrations, contract tests, stated
  independent deployment plus justification). Rejected alternatives with reasons, assumptions, pending decisions
  (Alembic, outbox/inbox, audit trail, authorization policy, tenant membership, unsupported authentication,
  end-to-end tests), signals and notes. Explicit `style_preference` honoured with a warning. No DDD, CQRS, event
  sourcing, microservices or hexagonal structure by default.

### Blueprint and generator (`fastapi_design/blueprint.py`, `generator.py`)

- Pure blueprint per style; app factory with lifespan, RFC 9457 problem details, pydantic-settings with a
  `current_settings` dependency, API-key authentication (constant-time, `SecretStr` without default), per-module
  schemas (`extra="forbid"`), models (dataclasses or SQLAlchemy 2 mapped classes with `tenant_id`), repository
  Protocol plus in-memory or SQL implementation (tenant-scoped), services with one commit per use case,
  dependencies, routers with return annotations and status codes, ports (Protocols) and adapters (HTTP with
  timeout, recording) where integrations exist, import-linter contracts for modular layouts, tests through the
  HTTP boundary, `.audit-fastapi.json` declaring the design, `.env.example`, `.gitignore`, README.
- Only the operations declared for an entity are generated. Output is deterministic (sorted, no timestamps).
- Writer rules: target created when missing; non-empty target refused without `--allow-existing` (adds missing
  files only); differing files are conflicts never replaced without `--overwrite`; nothing deleted; atomic writes;
  symlinked/file targets and paths escaping the target refused; manifest `.fastapi-architect/generation.json`
  (`fastapi-architect/generation/1.0`) with the contract digest, tool version, style and every file's sha256;
  `verify_manifest` reports `identical` / `modified` / `missing`.
- The generator refuses `oauth2_jwt` and `session` authentication (accepted by the contract, not generated).

### Verification and report (`fastapi_design/verify.py`, `report.py`, `pipeline.py`)

- Static audit of the generated target with `fastapi_audit.engine.audit_project`.
- Generated tests run only through `select_executor` with an explicit `Authorization` (trust `fixture` or
  `trusted` with a scope; `untrusted` fails closed) and the runner adapters of v1.9 (nonce-bound evidence); the
  generated target is the workspace project (digest compared before and after).
- `architecture_report/1.0` as JSON, text or markdown: contract summary with provenance counts, recommendation,
  layout, generation result, audit, tests, authorization, next steps.

### CLI

- `plugins/fastapi-architect/skills/fastapi-architect/scripts/design_fastapi.py` and the root shim
  `scripts/design_fastapi.py`: `SPEC --validate-only | --target DIR [--allow-existing] [--overwrite] [--dry-run]
  [--no-audit] [--authorize-execution --trust ... --trust-scope DIR --test-command CMD --timeout --env-passthrough
  --allow-command --executor] [--output FILE] [--format text|json|markdown] [--strict]`; exit codes 0/1/2.

### Examples

- `plugins/fastapi-architect/examples/specs/inventory-service.json` (small, in-memory, API key → layered) and
  `orders-platform.json` (medium, PostgreSQL with migrations, multi-tenant, two outbound integrations, contract
  tests → hexagonal with ports in `orders` and `billing`).
- `plugins/fastapi-architect/examples/generated/inventory-service/`: the backend generated from the first contract
  (28 files, manifest included), audited clean, its 9 generated tests passed through the trust model with
  `pytest-junit` evidence. A test asserts the snapshot equals a fresh generation.

### Auditor precision change (facts pass)

- Router dependencies now propagate through `X.include_router(Y)` edges to a fixpoint (any file order, any depth,
  names expanded to every import form), so routes mounted below `APIRouter(dependencies=[...])` are recognised as
  authenticated. Regression fixture `tests/fixtures/regressions_v20/nested_routers` with a negative control
  (`tests/test_audit_v20.py`). Measured with `scripts/audit_precision.py --against HEAD`: per-rule tp/fp/fn/tn
  identical on truth16, truth17 dev and acceptance, truth18 dev and acceptance (holdout not re-scored);
  calibration projects unchanged (plataforma-citas-medicas 52 → 52, agentforge-notify 15 → 15 findings).

## Increment 2: Persistence & Contract Hardening

### PostgreSQL and Alembic (generator)

- `core/db.py`: declarative base with a naming convention (`pk_`, `uq_`, `fk_`, `ix_`), async engine, request-scoped
  session that rolls back on any error. Services own the unit of work: repositories only stage changes, the service
  flushes and commits once per use case and turns an `IntegrityError` into a 409 `application/problem+json` after
  rolling back.
- Models: `UniqueConstraint(tenant_id, id)` and `ForeignKeyConstraint`s from declared `references`, composite
  `(tenant_id, <fk>)` between tenant tables, indexes on tenant and foreign-key columns.
- Alembic: `alembic.ini` (credential-free fallback URL), `migrations/env.py` (async engine, URL from
  `<PKG>_DATABASE_URL`, every model registered, `compare_type=True`), `script.py.mako`, and
  `migrations/versions/<rev>_initial_schema.py` generated from the contract in dependency order with explicit
  constraint names; the revision id is derived from the contract digest. `create_all` is never called.
- Generated suite: `migrated` refuses a non-empty database, runs `alembic upgrade head`, truncates after each test
  and downgrades at the end; `test_migrations.py` asserts one head, an empty `compare_metadata`, constraints by
  name and repeatable up/down; parents are created through their own API (`create_<entity>` helpers imported
  across test modules); 409 with no row persisted for a missing reference; 409 for a cross-tenant reference.
- `pyproject.toml` pins `sqlalchemy[asyncio]`, `asyncpg`, `alembic`; `.env.example` and settings carry placeholders
  only; the generated code passes its own ruff configuration (E, F, I, B, UP, ASYNC at 120 columns).

### Contract 1.1 and tenant guarantees

- `fastapi-architect/spec/1.1` (1.0 documents still accepted): `references`/`on_delete` on uuid fields (the target
  must be in the same module or in a declared `depends_on`; `set_null` needs `required: false`) and
  `security.multi_tenant.binding` (`none` | `api_key_registry`; the latter needs `authentication: api_key`).
- `api_key_registry`: `settings.api_keys` maps each key to one tenant; `require_api_key` authenticates in constant
  time against every key; `require_tenant_member` resolves the tenant path parameter and refuses any other tenant
  with 403; routers take the bound principal, never a bare `tenant_id`. The auditor sees the membership dependency
  on every tenant route (`authorization_visible`).
- A multi-tenant contract without a binding validates but is **refused** for generation (`generation_refused`
  explains why); `--validate-only` still reports.
- Report `guarantees` block: requested design, declared strategy, implemented strategy
  (`shared_schema_application_scoping`), principal-tenant binding, storage isolation, `row_level_security: false`,
  `schema_or_database_per_tenant: false`, authentication, resource authorization (not generated), persistence
  (migrations, `create_all_used: false`, 409 on constraint violation), execution (copy, process hygiene) and
  `not_claimed`. Negative controls in `tests/test_design_v20.py` (`GuaranteesTests`, `Schema11Tests`).

### Route composition (auditor)

- `facts.py` records `APIRouter(prefix=...)`, `include_router(..., prefix=...)` and applications bound to
  `FastAPI(...)` (module level or inside factories) and composes effective mount prefixes to a bounded fixpoint;
  `contracts.py` adds `effective_paths` and `path_resolution` (`resolved`, `ambiguous`, `unresolved`,
  `unmounted`) to every route contract; `public_routes` and `route_auth_required` match effective paths as well
  as router-local ones (every mount must match; nothing is matched for unresolved routers). Messages and
  fingerprints are unchanged. Regression fixture `tests/fixtures/regressions_v20/route_composition` (nested
  prefixes, a router mounted twice, a dynamic prefix, an app route). Corpora and calibration projects identical
  before and after (`scripts/audit_precision.py --against HEAD`; holdout not re-scored).
- Generated projects declare full public paths again: `/health` and `/api/v1/*` when there is no authentication.

### Verification in a copy

- `run_generated_tests` now runs in `Workspace.create(target)` (a copy without `.git`, environments or caches), as
  the executor's guarantees state, and reports `workspace.python_files_changed_by_the_run`. The opt-in
  `tests/test_design_v20_postgresql.py` (`FA_DESIGN_PG=1`, `tests/_pg_support.py` throwaway cluster) generates
  `orders-platform`, runs its suite with `uv run --group dev pytest` through the trust model with `DATABASE_URL`,
  `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` passed through, asserts the evidence (39 tests executed, 0 failed, 0
  skipped, nonce matched, copy unchanged, database left with `alembic_version` only), and runs a negative control
  (a mutated model makes `test_migrated_schema_matches_the_models` fail). Evidence and versions:
  `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.

### Examples and docs

- `examples/generated/orders-platform/` is committed next to `inventory-service/` (both asserted equal to a fresh
  generation); `examples/generated/README.md` and `VERIFICATION.md`; `docs/guides/generated-postgresql-projects.md`;
  `references/design-contracts.md` (schema 1.1, guarantees matrix, limits), `docs/architecture/design-engine-v2.md`
  (increment 2 record), structure validator extended.

## Increment 3: Safe Project Evolution

### Project discovery (`fastapi_design/discovery.py`, `evolve_fastapi.py discover`)

- Static model of an existing FastAPI project, by parsing only (no import, no execution): package and source root,
  layout (`layered`, `modular`, `unknown`; reported, never converted), business modules, routers with own and
  effective prefixes, routes with effective paths and authentication (from the audit contracts), pydantic models
  and settings, SQLAlchemy models (through resolved declarative bases), services, repositories, dependency aliases,
  lifespan, Alembic chain (revisions, heads, unknown parents, historical files changed since generation), declared
  contracts, and the ownership of every file against the manifest (`managed_unchanged`, `managed_modified`,
  `unmanaged`, `missing_managed`; `unknown` for every file without a manifest). Uncertainties are listed instead
  of guessed.

### Manifest 1.1 (`fastapi-architect/generation/1.1`)

- Adds `spec_document` (the contract, digest-checked), `migrations` (revision, parent, file, tables) and
  `history` (operations) to the file digests; 1.0 manifests are still read. Both committed examples regenerated.

### Evolution contract and plan (`fastapi_design/evolution.py`)

- `fastapi-architect/evolution/1.0`: project (path, package, expected baseline digest, optional baseline contract
  for 1.0 manifests), `add_modules` (same shape as the design contract), invariants, `files.allow_modify` and
  `files.preserve`. The modules are merged into the baseline contract and the whole contract is validated again;
  errors point at `evolution.add_modules[i]...`.
- Plan: preconditions (manifest, baseline, digest, package, layout, missing files, linear Alembic chain with the
  recorded head, historical migrations unchanged), style pinned to the existing layout (the advisor's new opinion
  is a warning), files to create / modify / keep, preserved manual changes, conflicts (`file_in_the_way`,
  `manual_change_on_integration_point`, `unmanaged_file_in_the_way`, `not_allowed_to_modify`,
  `preserved_file_would_change`, `missing_managed_file`, `unsafe_path`), route collisions, public-contract changes,
  the additive migration (PostgreSQL), risks, required tests and unified diffs. Same contract twice → `noop`;
  an existing module with a different definition → blocked.

### Safe writer (`generator.write_files`)

- Two-phase atomic writes shared by generation and evolution: every expected digest (or absence) re-checked
  immediately before writing (concurrent changes abort before any write), temporaries then renames, cleanup on
  failure, unsafe paths and writes through symlinks refused, nothing deleted, nothing silently overwritten.

### Verification in a copy and states

- `apply` applies the plan to a copy, audits it and compares by fingerprint with the project (new high/critical
  findings reject), checks that every existing route keeps its path and authentication, checks that nothing
  outside the plan changed, runs the tests only under the trust model (the authorised scope covers the project and
  is moved to the copy; `untrusted` fails closed), and writes the project only after `verified` or `audit_clean`.
  States: `planned`, `noop`, `blocked`, `verified`, `audit_clean`, `inconclusive`, `rejected`.

### Verified

- `inventory-service` + `suppliers` (layered, in-memory): 7 files created, 4 regenerated, 18 generated tests
  (9 existing + 9 new) passed through the trust model with nonce-bound evidence, applied; second run `noop`; a
  manual change on `services/catalog.py` preserved by a third evolution; a hand-edited `api/router.py` blocks.
- `orders-platform` + `shipments` (hexagonal, PostgreSQL, multi-tenant): additive migration
  `77d681cb65a8` chained after `9dab920675b3`, composite tenant foreign key to `orders`, 50 generated tests passed on
  a throwaway PostgreSQL 16 through the trust model (opt-in `FA_DESIGN_PG=1`), historical migration byte-identical,
  database left with `alembic_version` only. Evidence: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.

### Tests, examples, docs

- `tests/test_evolve_v20.py` (26 tests: discovery of generated and hand-written projects, contract validation,
  plan preview, style pinning, unknown project, stale baseline, duplicate module and routes, cycles and bad
  references with contract paths, manual changes on integration points and elsewhere, unexpected managed content,
  files in the way and preserved files, symlinked project, writer escapes / symlinks / concurrent changes, write
  error, apply without authorization, test command without authorization, untrusted profile, the vertical case
  with tests and idempotency, failed verification, additive migration plan, modified historical migration and two
  heads, CLI exit codes); the opt-in PostgreSQL module gains the evolved backend run.
- `examples/evolutions/{inventory-add-suppliers,orders-add-shipments}.json`,
  `references/evolution-contracts.md`, `docs/guides/evolving-generated-projects.md`, the increment 3 section of
  `docs/architecture/design-engine-v2.md`, `evolve_fastapi.py` (skill + root shim), structure validator extended.

## Increment 4: Safe Evolution & Project Adoption

### Recoverable writer (`fastapi_design/journal.py`)

- `prepare` (preconditions re-checked, new content and backups staged under `.fastapi-architect/ops/<id>/`,
  journal with the plan identity: sha256 of paths, expected and new digests, kind and description), `publish`
  (journal `publishing`, preconditions re-checked once more, per-file temporary + `os.replace`, every landing
  journaled, then `published`, cleanup, journal deleted), `recover` (files classified `published` / `pending` /
  `conflict` by digest; `rollback` restores backups and removes created files, `forward` publishes the pending
  ones; conflicts are never touched and keep the journal as `needs_attention`; progress journaled, resumable,
  idempotent). Limits stated: atomic per file, recoverable across files, no global atomicity, same filesystem.
- Fault injection (`FaultInjector`: before publication, after the n-th file, after all, before cleanup, during
  recovery). Every write of `apply` and `adopt` goes through it; a pending journal blocks new plans;
  `evolve_fastapi.py recover PROJECT [--forward] [--op ID]`.

### Evolution contract 1.1 (`fastapi-architect/evolution/1.1`; 1.0 still read)

- `modify_modules`: `add_depends_on`, `entities[].add_fields` (nullable, or required with a `default` that becomes
  the schema, model and server default), `entities[].add_operations`; for adopted projects `add_endpoints`
  (method, path, name, summary, literal response) and `add_fields` on a pydantic class. Every change is classified
  `compatible`, `potentially_incompatible` (required field without default; NOT NULL column or NOT NULL foreign key
  on an existing table: blocked with the safe form spelled out) or `unsupported` (removals, renames, type changes,
  new entities in an existing module: contract error). The changed module's own files are regenerated from the
  merged contract; a manual change on them blocks. Field `default` added to the design contract (schema 1.1).

### Additive column migrations

- The lineage carries the baseline contract; the delta revision emits `add_column` (nullable, or NOT NULL with a
  server default) and `create_foreign_key` + index for nullable references next to `create_table` for new
  tables; downgrade drops them (data loss on rollback stated under risks). No `alter_column`, no drop of existing
  objects, no data migration, no merge of heads: additive only, not expand/contract.

### Structural transforms (`fastapi_design/transforms.py`)

- `insert_function`, `insert_class_field`, `ensure_import`, `compose`: `ast` analysis, anchored insertion into the
  original text (end of module, end of class body, end of the import block), AST comparison before and after
  (every pre-existing top-level node unchanged and in order, exactly the inserted nodes added), unified diff and
  evidence; refusals: duplicate symbol, duplicate route (method and path on the router), duplicate field, router or
  class bound more than once (ambiguity), unparsable target or snippet. Standard library only; a CST dependency
  would only be justified to rewrite existing nodes, which is refused.

### Adoption (`fastapi_design/adoption.py`, `fastapi-architect/adoption/1.0`)

- `propose` (candidates per router with routes and related files, unassigned files, uncertainties; nothing
  adopted), the adoption contract (modules with router and exact files; no file in two modules), `adopt`
  (refuses generated projects, unknown structures, missing or symlinked files, unparsable files, routers not bound
  exactly once; writes only `.fastapi-architect/adoption.json` with digests, routers, routes, boundary and history).
  Discovery reports `adopted_unchanged`, `adopted_modified`, `adopted_missing`, `unadopted`. Evolution of adopted
  modules: only `add_endpoints` / `add_fields` through the transforms, only on adopted files unchanged since
  adoption; unadopted files are protected; duplicates and ambiguity block.

### Verified

- Scenario A, generated: `inventory-service` + `inventory-add-product-fields.json` (two fields on Product; 9 tests
  through the trust model, applied, `noop` on the second run); `orders-platform` +
  `orders-add-customer-columns.json` on a throwaway PostgreSQL 16 (additive revision `e2dc0f95b7da` after
  `9dab920675b3`: nullable `phone`, NOT NULL `tier` with server default, `update` operation on orders; 40 tests
  passed through the trust model; backfill of existing rows by the server default and clean downgrade; a
  conflicting column created by hand makes the upgrade fail with the schema left at the previous revision and the
  verification never `verified`).
- Scenario B, hand-written: `tests/fixtures/adoption20/handmade_shop` proposed, `items` adopted, an endpoint and
  an optional field added through anchored transforms, the project's own 3 tests passed through the trust model,
  `users.py` byte-identical; unadopted module, manual change after adoption, duplicate route/symbol/field,
  ambiguous class, concurrent edit after the plan and a failing project test all refused.
- Recoverable writer: interrupted after 2 of 6 files → rollback to the original tree or forward to the complete
  one; interrupted recovery resumed; conflict left alone; concurrent change before publication → nothing
  published; unsafe paths, symlinks and staging I/O errors leave nothing behind.

### Tests, examples, docs

- `tests/test_evolve_v20_recovery.py` (17 tests) and `tests/test_evolve_v20_adoption.py` (22 tests); the opt-in
  PostgreSQL module gains three tests (column evolution, backfill, migration failure). Fixture
  `tests/fixtures/adoption20/handmade_shop` with `adopt-items.json` and `items-add-stats.json`; examples
  `inventory-add-product-fields.json`, `orders-add-customer-columns.json`; `references/evolution-contracts.md`,
  `docs/guides/evolving-generated-projects.md`, the increment 4 section of `docs/architecture/design-engine-v2.md`,
  `examples/generated/VERIFICATION.md`, structure validator extended.

## Increment 5: Identity, Authorization & Tenant Isolation

### Security design contract (`fastapi-architect/spec/1.2`; 1.1 and 1.0 still accepted)

- `security.oidc` (issuer, audience, asymmetric `algorithms`, `jwks_url`, `leeway_s`, `jwks_cache_s`, `claims`
  subject/tenant/roles), the authorization object (`model` none/roles/ownership/roles_and_ownership, `roles`,
  `operations` operation -> roles, `ownership` visibility tenant/owner + `admin_roles`), `owned` entities (the
  generated `owner_id`), the tenant strategy `shared_schema_application_scoping` (the 1.2 default), the
  `principal_claim` binding, `multi_tenant.rls` (`app_role`, `context_key`) and `security.audit`.
- Every impossible or unsupported combination is a contract error with its JSON path (RLS on memory or without a
  binding, `principal_claim` with API keys, roles on a shared API key, `HS*`/`none`, http issuer or JWKS, owned
  entities without an ownership model and vice versa, undeclared roles, 1.2 keys in 1.0/1.1 documents). A 1.0/1.1
  document keeps its meaning: the `authorization` string stays a declaration and a declared `shared_schema_rls`
  stays application scoping; the engine never upgrades a contract behind its author's back.
- The `guarantees` block now states `identity`, `authorization_model`, `audit_logging`, the RLS context scope and
  the infrastructure-failure answer; `not_claimed` is computed from what was built.

### Authenticated identity (`core/security.py`)

- `oauth2_jwt` is generated: `JwksSource` (https only, cache bounded by `jwks_cache_s`, one refresh per unknown key
  id and never more often than every 30 s, static JWKS only when `ENVIRONMENT=test`), `verify_token` (algorithm
  allow-list checked before any key lookup, signature, `iss`, `aud`, `exp`/`nbf`/`iat`, required claims) and a
  `Principal` built from verified claims only; an unreachable provider is 503, not 401. PyJWT with cryptography is the
  only new generated dependency. No refresh tokens, sessions or token issuance.
- API keys become grants (`tenant_id`, `subject`, `roles`); a bare tenant id is still read. A shared API key stays
  what it is: no caller identity, so no policy can be declared on it.

### Resource authorization (`core/authz.py`)

- Per-operation roles in route dependencies (`CanCreate`, `CanGet`, `CanList`, `CanUpdate`, `CanDelete`: 403 before
  the use case runs); ownership in the service (`ensure_owner`) and in the repository query (`read_scope`): visibility
  `tenant` reads tenant-wide and lets only the owner or an admin modify (403), visibility `owner` scopes reads too
  (404). Routers only carry the principal; `owner_id` is never taken from the client (422). Denials are logged on
  request. Status codes are kept apart: 401 credential, 403 denied, 404 not visible, 422 malformed, 503
  infrastructure.

### Row-level security (`strategy: shared_schema_rls`, PostgreSQL, opt-in)

- Every tenant table is protected in the migration that creates it: `GRANT` row privileges to the application role,
  `ENABLE` + `FORCE ROW LEVEL SECURITY`, one `USING`/`WITH CHECK` policy on
  `NULLIF(current_setting('<context_key>', true), '')::uuid`. The migration verifies on the live connection that the
  role exists and is neither superuser nor `BYPASSRLS` and refuses otherwise (transactional DDL: no table left
  behind); roles are provisioning, never created with a default password.
- Two URLs: `<PKG>_DATABASE_URL` (the application role) and `<PKG>_MIGRATIONS_DATABASE_URL` (the owner).
  `get_tenant_session` sets the context with `set_config(..., true)` for the request transaction only; no
  session-level `SET`; commit, rollback and pool return clear it. Evolution adds the protection to new tables in
  the additive revision; `security`/`persistence` are not evolution operations; a 1.0/1.1 project that declared
  RLS keeps application scoping and the plan says so.
- The generated `tests/test_rls.py` proves the isolation through the restricted role with raw SQL (policies,
  role, no context, cross-tenant writes, context after commit/rollback/pool reuse, rollback residue, invalid
  context, the role trying to lift the protection, 24 concurrent requests of two tenants, 503 on an unreachable
  database).

### Verified

- Vertical slice 1, `examples/specs/team-notes.json` (OIDC + `principal_claim` + `roles_and_ownership`, in memory):
  54 generated tests through the trust model (legitimate access, other member per policy, other tenant 403 and
  404, nonexistent, forbidden operation, invalid and tampered tokens, modification without permission, admin
  override, the OIDC negative controls); audited with zero findings and every protected route showing its
  authorization. The single-tenant owner-scoped `journal` fixture (ES256, visibility `owner`) passes its suite too.
- Vertical slice 2, `examples/specs/tenant-projects.json` (API-key grants + `roles_and_ownership` + RLS): 51
  generated tests on a throwaway PostgreSQL 16 as the restricted role `fa_app` (12 RLS demonstrations, concurrency,
  ownership, roles, cross-tenant references); a `BYPASSRLS` role and a missing role make the migration refuse with
  no table behind; the evolution that adds `milestones` protects the new table and passes 64 tests. Evidence and
  costs: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.
- Auditor integration: both generated projects audit with nothing above `info` and `authorization_unknown: 0`;
  negative controls: a route stripped of its policy and principal is reported through
  `CONTRACT_MUTATION_AUTHZ_REQUIRED` (the generated declared contract), a session-level `SET` through
  `SEC_RLS_SET_NOT_LOCAL`; no auditor rule or regex changed, corpora untouched.

### Tests, examples, docs

- `tests/test_design_v20_security.py` (27 tests), `tests/test_design_v20_postgresql.py` + 3 opt-in tests
  (`RowLevelSecurityBackendTests`), fixtures `oidc_owner_scoped.json`, `rls_claim_binding.json` and four invalid
  `security12_*.json` contracts; snapshots `examples/generated/team-notes` and `tenant-projects` (byte-equal to a
  fresh generation, audited); `examples/evolutions/tenant-projects-add-milestones.json`; manifest 1.2 with the
  `security` record. Docs: `references/design-contracts.md` section 8, `docs/guides/identity-authorization-and-rls.md`,
  the architecture record, this file.

## Compatibility

- 59 rules, report schema 1.7, JSON/SARIF outputs, fingerprints, suppressions, declared contracts, the opt-in loop
  policy, `audit_diff.py`, the remediation run (`remediation_run/1.2`) and the remediation CLI are unchanged.
  `TOOL_VERSION` is `2.0.0` (manifests updated); the only test touched for that is the version assertion.
- The design engine is a separate package and CLI; the audit and remediation entry points do not import it.

## Tests and validations

- `tests/test_design_v20.py` (67 tests: validation positives and negatives with JSON paths, advisor rules
  including microservices only when explicit, blueprint determinism and layouts, generator safety and idempotency,
  pipeline and trust-model denials, the generated tests executed through the trust model when fastapi/httpx/pytest
  are importable, report renderings, snapshot equality for the four examples, CLI exit codes; increment 2: schema
  1.1, tenant binding refusal, Alembic and foreign keys, guarantees, ruff on the generated projects when ruff or
  uvx is available), `tests/test_audit_v20.py` (12 tests: router propagation and route composition),
  `tests/test_evolve_v20.py` (26), `tests/test_evolve_v20_recovery.py` (17), `tests/test_evolve_v20_adoption.py`
  (22), `tests/test_design_v20_security.py` (27) and the opt-in `tests/test_design_v20_postgresql.py` (9 tests,
  `FA_DESIGN_PG=1`). Full suite: 532 tests on Python 3.13 (10 skipped: the opt-in PostgreSQL tests and ruff) and
  3.9 (22 skipped: plus the generated suites, which need Python 3.12+).
- Fixtures: `tests/fixtures/design20/specs/{valid,invalid}` (9 + 15 contracts),
  `tests/fixtures/regressions_v20/{nested_routers,route_composition}`, `tests/fixtures/adoption20/handmade_shop`.
- Full unit suite on Python 3.13 and 3.9, `tests/test_docs_links.py`, `scripts/validate_structure.py`
  (extended with the v2.0 artefacts), `claude plugin validate`, `git diff --check`: see the increment report.

## Known limits (after increment 5)

- Generated projects: new modules and additive changes to existing ones (nullable or defaulted fields,
  operations, dependencies); no removals, renames, type changes, new entities in an existing module, or layout
  migrations; integration points and changed module files are regenerated (a manual change on them must be moved
  into the contract first).
- Adopted projects: handlers returning a literal and optional fields on pydantic classes, inside adopted files
  only; existing handlers are never rewritten; no generated modules or persistence for adopted projects.
- Migrations: one additive revision per evolution (tables, nullable or defaulted columns, nullable foreign keys);
  expand/contract, column changes, data migrations and merges of heads are not supported; a downgrade drops the
  added columns with their data.
- Writes: atomic per file and recoverable across files through the journal; no global atomicity; the staging
  directory must share the project's filesystem; a writer that ignores the journal is not protected against.

- Only the initial migration is generated; later migrations are the author's (`alembic revision --autogenerate`
  works on the generated metadata). `uv.lock` is not generated (resolution needs the index).
- Security: session authentication, refresh tokens, token issuance, public resources inside an authenticated API,
  cross-tenant administration, schema or database per tenant are not generated; the OIDC provider is trusted for
  the claims it signs; a project generated from a 1.0/1.1 contract keeps application scoping even if it declared
  `shared_schema_rls` (moving to RLS is a new design, not an evolution); the throwaway-cluster verification uses the
  bootstrap superuser as the migrations owner (FORCE is checked through the catalog flags and the restricted role).
- Generated PostgreSQL suites other than `orders-platform` and `tenant-projects` are generated and linted but not
  executed by the plugin's tests; the opt-in verification needs local PostgreSQL binaries and uv.
- The local executor is process hygiene, not a sandbox; generated code runs with the operator's privileges when
  authorised, and `uv run` reaches the network when the cache misses.
- Route composition follows string-constant prefixes through `include_router`; prefixes built at runtime are
  reported `unresolved`, and `root_path`/mounted sub-applications are not composed.

## Roadmap for the next v2.0 increments

1. Expand/contract migrations with explicit data steps (backfill scripts, NOT NULL after backfill, column renames
   through add-copy-drop), and richer transforms for adopted modules (handlers with parameters and bodies declared
   in the contract, imports of project symbols) under the same AST evidence.
2. Operations files from `assets/` (Dockerfile, CI, pre-commit) and the reliability patterns (outbox, workers)
   as opt-in generation guarded by decisions in the contract.
3. Sub-application mounts and `root_path` in the auditor's route composition.
4. Security follow-ups: a reviewed migration path from application scoping to RLS for existing projects (with
   its own tests), background jobs with an explicit tenant context, and public read operations inside an
   authenticated API as a declared decision.
