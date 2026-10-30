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

## 1. Specification (`fastapi-architect/spec/1.0`)

Plain JSON, validated strictly like `.audit-fastapi.json`: unknown properties are errors (annotations prefixed
`x-` are allowed), identifiers are checked, enumerations are closed, and every problem is reported with its JSON
path. Validation collects all errors instead of stopping at the first one.

| Key | Content | Default when omitted |
|---|---|---|
| `schema` | must be `fastapi-architect/spec/1.0` | required |
| `name` | service name (letters, digits, space, `.`, `_`, `-`) | required |
| `package` | python package name (lowercase identifier) | derived from `name` |
| `description` | free text | `<name> backend` |
| `scope` | `size` small/medium/large, `team_size`, `expected_rps`, `data_volume` | small, 2, 50, small |
| `functional.modules[]` | `name` (identifier, not reserved), `description`, `depends_on` (other modules, acyclic), `entities[]` | required, at least one |
| `entities[]` | `name` (CapitalizedWord), `fields[]`, `operations` ⊆ create/get/list/update/delete | all operations |
| `fields[]` | `name` (identifier; `id`, `tenant_id`, `created_at`, `updated_at` are generated and cannot be declared), `type` str/int/float/bool/uuid/datetime/date/decimal, `required`, `max_length` (str only), `min_value`/`max_value` (numeric only), `description` | required true |
| `nonfunctional` | `availability`, `latency_p95_ms`, `consistency` strong/eventual, `auditability`, `compliance[]` | 99.5%, 300, strong, false, [] |
| `persistence` | `kind` memory/postgresql, `migrations` (postgresql only) | memory, false |
| `security` | `authentication` none/api_key/oauth2_jwt/session, `authorization` none/role/resource (needs authentication), `multi_tenant` {`enabled` (needs authentication), `strategy` shared_schema_rls/schema_per_tenant/database_per_tenant, `tenant_param`} | none, none, disabled / shared_schema_rls / `tenant_id` |
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
must exist and be acyclic; an accepted decision must state its rationale; module and entity names are unique.

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
  own settings), `core/errors.py`, `core/security.py` (api_key), `core/db.py` (postgresql: async engine,
  request-scoped `AsyncSession`, the **service** commits once per use case);
- per module: schemas (`extra="forbid"`, constraints from the fields), models (dataclasses in memory, SQLAlchemy 2
  mapped classes for postgresql, `tenant_id` column when multi-tenant), repository Protocol plus the chosen
  implementation (tenant-scoped on every call), service with the declared use cases, dependencies (composition),
  router with return annotations and status codes; **only the declared operations** are generated;
- `tests/conftest.py` (one app per test with fresh in-memory state, recording adapters for ports; postgresql tests
  skip without `DATABASE_URL`) and `tests/test_<module>.py` through the HTTP boundary: 201 on create, 422 on
  invalid payload and on unknown properties, 200/404 on get with `application/problem+json`, pagination with
  limits, update, delete, 401 without the API key, and the port notification when a port exists;
- `.audit-fastapi.json` declaring the design for the auditor: `public_routes` (`/*` when there is no
  authentication, `/health` otherwise), `tenant_params`, `contracts.forbidden_imports` between modules that do not
  declare a dependency, `contracts.commit_layers: ["service"]` for postgresql, `contracts.route_auth_required`
  when authenticated; `.env.example`, `.gitignore`, a README with the rationale and the pending decisions.

The generator **refuses** contracts whose `authentication` is `oauth2_jwt` or `session`: the contract accepts
them, this increment does not generate them, and a non-functional security stub is worse than a pending decision.

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
  clean: router-level dependencies are propagated through `include_router` since v2.0 (see the release notes), the
  declared public routes and tenant parameters are honoured, and the route contracts record what remains open
  (`authorization_unknown` for tenant routes until the author binds the tenant to the principal).
- **Tests**: only with `--authorize-execution` and a trust profile accepted by an executor of the v1.9 trust
  model. The local executor provides process hygiene (no shell, allow-listed programs, fresh HOME, reduced
  environment, closed stdin, timeouts, CPU/file-size limits), not a sandbox; it refuses `untrusted`. The run is
  bound to a runner adapter (`pytest-junit`, `unittest-json`) that produces structured evidence; the generated
  project itself is the workspace project (it is the output of the tool, not a foreign project) and its digest is
  compared before and after. Without authorization the report says `tests: not executed` and why.

## 5. Report (`architecture_report/1.0`)

```json
{
  "schema": "architecture_report/1.0",
  "tool": {"name": "fastapi-architect design", "version": "2.0.0"},
  "spec": {"name", "schema", "digest", "source", "package", "modules", "entities", "persistence", "authentication", "multi_tenant", "explicit_paths", "assumed_paths", "document"},
  "recommendation": {"style", "rationale", "rejected", "assumptions", "pending", "signals", "ports_modules", "warnings", "persistence_notes", "security_notes", "testing_notes"},
  "layout": ["tree lines"], "files": ["relative paths"], "blueprint_notes": [],
  "generation": {"target", "dry_run", "written", "unchanged", "conflicts", "manifest"} | null,
  "generation_refused": "reason" | null,
  "verification": {"audit": {"schema_version", "files_scanned", "findings_total", "by_severity", "findings", "contracts", "partial_analysis"}, "tests": {"ran", "executor", "run", "denied_reason", "denied_code"}} | null,
  "authorization": {"execution", "trust", "scope", "declared_by", "technical_guarantee": false},
  "next_steps": [], "note": "..."
}
```

`--format text` prints a compact summary, `--format markdown` the full report (recommendation, rejected
alternatives, assumptions, pending decisions, layout, generation, audit, tests). JSON is sorted and stable.

## 6. Limits of this increment

- Generated: layered, modular monolith and hexagonal layouts; in-memory and PostgreSQL persistence (no Alembic
  environment yet); API-key authentication; tenant scoping by path parameter (membership binding is a pending
  decision the auditor keeps asking about); HTTP and recording adapters for ports.
- Not generated: OAuth2/JWT or session authentication, authorization policies, outbox/inbox, audit trails,
  background workers, Docker/CI files (see `assets/`), end-to-end tests, service splitting for `microservices`.
- The auditor matches `public_routes` against router-local paths (prefixes added by `include_router` are not
  composed yet); the generated declarations take that into account.
