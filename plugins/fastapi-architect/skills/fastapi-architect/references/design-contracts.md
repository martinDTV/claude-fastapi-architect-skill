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

## 1. Specification (`fastapi-architect/spec/1.1`; `1.0` documents are still accepted)

Plain JSON, validated strictly like `.audit-fastapi.json`: unknown properties are errors (annotations prefixed
`x-` are allowed), identifiers are checked, enumerations are closed, and every problem is reported with its JSON
path. Validation collects all errors instead of stopping at the first one. Schema 1.1 (increment 2) adds
`references`/`on_delete` on uuid fields and `security.multi_tenant.binding`; a 1.0 document is a 1.1 document
without them.

| Key | Content | Default when omitted |
|---|---|---|
| `schema` | `fastapi-architect/spec/1.1` or `fastapi-architect/spec/1.0` | required |
| `name` | service name (letters, digits, space, `.`, `_`, `-`) | required |
| `package` | python package name (lowercase identifier) | derived from `name` |
| `description` | free text | `<name> backend` |
| `scope` | `size` small/medium/large, `team_size`, `expected_rps`, `data_volume` | small, 2, 50, small |
| `functional.modules[]` | `name` (identifier, not reserved), `description`, `depends_on` (other modules, acyclic), `entities[]` | required, at least one |
| `entities[]` | `name` (CapitalizedWord), `fields[]`, `operations` ⊆ create/get/list/update/delete | all operations |
| `fields[]` | `name` (identifier; `id`, `tenant_id`, `created_at`, `updated_at` are generated and cannot be declared), `type` str/int/float/bool/uuid/datetime/date/decimal, `required`, `max_length` (str only), `min_value`/`max_value` (numeric only), `description`, `references` (`"module.Entity"` or `"Entity"`, uuid only: a foreign key in SQL persistence; the target must be in the same module or in a declared `depends_on`), `on_delete` restrict/cascade/set_null (`set_null` needs `required: false`) | required true, on_delete restrict |
| `nonfunctional` | `availability`, `latency_p95_ms`, `consistency` strong/eventual, `auditability`, `compliance[]` | 99.5%, 300, strong, false, [] |
| `persistence` | `kind` memory/postgresql, `migrations` (postgresql only) | memory, false |
| `security` | `authentication` none/api_key/oauth2_jwt/session, `authorization` none/role/resource (needs authentication), `multi_tenant` {`enabled` (needs authentication), `strategy` shared_schema_rls/schema_per_tenant/database_per_tenant, `tenant_param`, `binding` none/api_key_registry (how the authenticated principal is bound to the tenant it addresses; `api_key_registry` needs `authentication: api_key`)} | none, none, disabled / shared_schema_rls / `tenant_id` / none |
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
must state its rationale; module and entity names are unique.

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
  own settings; no credential has a default), `core/errors.py`, `core/security.py` (api_key: one shared key, or
  `api_key_registry`: `api_keys` maps each key to one tenant, `require_tenant_member` resolves the tenant path
  parameter and refuses any other tenant with 403), `core/db.py` (postgresql: async engine, declarative base with
  a naming convention, request-scoped `AsyncSession` that rolls back on errors; the **service** flushes and
  commits once per use case and turns an `IntegrityError` into a 409 after rolling back);
- for postgresql: `alembic.ini`, `migrations/env.py` (async, URL from `<PKG>_DATABASE_URL`, every model
  registered), `migrations/script.py.mako`, and `migrations/versions/<rev>_initial_schema.py` generated from the
  contract (tables in dependency order, explicit constraint names, indexes on tenant and foreign-key columns,
  revision id derived from the contract digest); `create_all` is never called;
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
  `application/problem+json`, pagination with limits, update, delete, 401 without the API key, the port
  notification when a port exists, 409 with no row persisted when a reference points at a missing row, and for
  multi-tenant contracts 403 for another tenant's path, empty listings across tenants and 409 when a reference
  points at another tenant's row; `tests/test_migrations.py` asserts one head, an empty `compare_metadata`
  (schema drift is detected), the constraints by name and repeatable up/down;
- `.audit-fastapi.json` declaring the design for the auditor: `public_routes` (`/health` and `/api/v1/*` when
  there is no authentication, `/health` otherwise; full paths, since the auditor composes `include_router`
  prefixes), `tenant_params`, `contracts.forbidden_imports` between modules that do not declare a dependency,
  `contracts.commit_layers: ["service"]` for postgresql, `contracts.route_auth_required` when authenticated;
  `.env.example` with placeholders only, `.gitignore`, a README with the rationale, the migration commands, the
  tenant model and the pending decisions. The generated code passes its own ruff configuration (E, F, I, B, UP,
  ASYNC at 120 columns).

The generator **refuses** contracts whose `authentication` is `oauth2_jwt` or `session`, and multi-tenant
contracts whose `binding` is `none`: the contract accepts them, this increment does not generate them, and a
route that accepts any `tenant_id` from any authenticated caller is not authorization. The refusal is reported
(`generation_refused`) and `--validate-only` still produces the recommendation and the guarantees block.

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
(`fastapi-architect/generation/1.0`) records the tool version, the spec name and digest, the style, the package and
the sha256 of every generated file, without timestamps, so re-running the same contract changes nothing and
`verify_manifest` can tell `identical`, `modified` and `missing` files apart.

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
  "guarantees": {"multi_tenant": {"requested", "strategy_declared", "strategy_implemented", "principal_tenant_binding", "storage_isolation", "row_level_security": false, "schema_or_database_per_tenant": false}, "authentication", "resource_authorization", "persistence": {"kind", "migrations", "create_all_used": false, "constraint_violation_on_commit"}, "execution", "not_claimed"},
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

| Aspect | Contract | Generated (increment 2) |
|---|---|---|
| Multi-tenant design requested | `security.multi_tenant.enabled` | `tenant_id` on every entity, tenant-scoped routes and repositories |
| Isolation strategy selected | `strategy` (shared_schema_rls, schema_per_tenant, database_per_tenant) | recorded; **implemented: `shared_schema_application_scoping`** (tenant filter on every query, composite foreign keys on PostgreSQL). RLS policies, schema or database per tenant are not generated and the report says `row_level_security: false` |
| Authenticated identity | `authentication` | `api_key` (shared key) or `api_key_registry` (each key bound to one tenant); OAuth2/JWT and sessions are refused |
| Principal-tenant binding | `binding` | `api_key_registry`: `require_tenant_member` compares the tenant bound to the key with the tenant path parameter (403 otherwise); `none`: generation refused |
| Resource authorization | `authorization` | not generated; recorded as a pending decision and reported as `resource_authorization: not generated` |
| Storage isolation | `strategy` | application scoping plus composite `(tenant_id, id)` foreign keys; a row can never reference another tenant's row; no database-level isolation |

Negative controls in the suite: a contract with `binding: none` validates, is refused for generation and never
gets `principal_tenant_binding: enforced`; a declared `shared_schema_rls` is never reported as implemented; the
generated tests prove the 403, the empty cross-tenant listings and the 409 on cross-tenant references.

## 7. Limits of this increment

- Generated: layered, modular monolith and hexagonal layouts; in-memory and PostgreSQL persistence with an Alembic
  environment and the initial migration; API-key authentication (shared key or per-tenant registry); tenant
  scoping with the registry binding; HTTP and recording adapters for ports.
- Not generated: OAuth2/JWT or session authentication, authorization policies, row-level security, schema or
  database per tenant, outbox/inbox, workers, Docker/CI files (see `assets/`), end-to-end tests, service
  splitting for `microservices`, later migrations (`alembic revision --autogenerate` works on the generated
  metadata), `uv.lock` (resolution needs the index; version ranges are explicit).
- Verified against PostgreSQL: `orders-platform` through the opt-in test (`FA_DESIGN_PG=1`) on a throwaway
  cluster; the evidence is in `plugins/fastapi-architect/examples/generated/VERIFICATION.md`. Other PostgreSQL
  contracts are generated and linted but their suites are not executed by the plugin's tests.
