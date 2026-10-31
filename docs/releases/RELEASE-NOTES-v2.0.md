# FastAPI Architect v2.0.0 — Architecture & Engineering Foundations

**Status: increments 1, 2 and 3 implemented and validated locally; pending human review; not published.**

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

## Compatibility

- 59 rules, report schema 1.7, JSON/SARIF outputs, fingerprints, suppressions, declared contracts, the opt-in loop
  policy, `audit_diff.py`, the remediation run (`remediation_run/1.2`) and the remediation CLI are unchanged.
  `TOOL_VERSION` is `2.0.0` (manifests updated); the only test touched for that is the version assertion.
- The design engine is a separate package and CLI; the audit and remediation entry points do not import it.

## Tests and validations

- `tests/test_design_v20.py` (67 tests: validation positives and negatives with JSON paths, advisor rules
  including microservices only when explicit, blueprint determinism and layouts, generator safety and idempotency,
  pipeline and trust-model denials, the generated tests executed through the trust model when fastapi/httpx/pytest
  are importable, report renderings, snapshot equality for both examples, CLI exit codes; increment 2: schema 1.1,
  tenant binding refusal, Alembic and foreign keys, guarantees, ruff on the generated projects when ruff or uvx is
  available), `tests/test_audit_v20.py` (12 tests: router propagation and route composition) and the opt-in
  `tests/test_design_v20_postgresql.py` (2 tests, `FA_DESIGN_PG=1`).
- Fixtures: `tests/fixtures/design20/specs/{valid,invalid}` (7 + 11 contracts),
  `tests/fixtures/regressions_v20/{nested_routers,route_composition}`.
- Full unit suite on Python 3.13 and 3.9, `tests/test_docs_links.py`, `scripts/validate_structure.py`
  (extended with the v2.0 artefacts), `claude plugin validate`, `git diff --check`: see the increment report.

## Known limits (after increment 3)

- Evolution adds whole modules to generated projects only: existing modules are not changed, hand-written projects
  are discovered but refused, layouts are never migrated, integration points are regenerated (a manual change on
  one must be moved into the contract first), one additive migration per evolution (no column changes, no data
  migrations, no merge of heads).

- Only the initial migration is generated; later migrations are the author's (`alembic revision --autogenerate`
  works on the generated metadata). `uv.lock` is not generated (resolution needs the index).
- Tenant isolation is application scoping plus composite foreign keys: no row-level security, no schema or
  database per tenant, no resource-level authorization. OAuth2/JWT and session authentication are refused.
- Generated PostgreSQL suites other than `orders-platform` are generated and linted but not executed by the
  plugin's tests; the opt-in verification needs local PostgreSQL binaries and uv.
- The local executor is process hygiene, not a sandbox; generated code runs with the operator's privileges when
  authorised, and `uv run` reaches the network when the cache misses.
- Route composition follows string-constant prefixes through `include_router`; prefixes built at runtime are
  reported `unresolved`, and `root_path`/mounted sub-applications are not composed.

## Roadmap for the next v2.0 increments

1. Evolving existing modules (new fields and operations) with additive column migrations, and adopting
   hand-written projects module by module under an explicit ownership declaration.
2. Authentication beyond API keys (OAuth2/JWT with the references' guidance) and resource-level authorization
   policies as declared decisions.
3. Row-level security as an opt-in strategy implementation (policies, session tenant context, the tenant-ledger
   discipline) so `shared_schema_rls` can be reported as implemented.
4. Operations files from `assets/` (Dockerfile, CI, pre-commit) and the reliability patterns (outbox, workers)
   as opt-in generation guarded by decisions in the contract.
5. Sub-application mounts and `root_path` in the auditor's route composition.
