# FastAPI Architect v2.0.0 — Architecture & Engineering Foundations

**Status: first increment, implemented and validated locally; pending human review; not published.**

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

## Compatibility

- 59 rules, report schema 1.7, JSON/SARIF outputs, fingerprints, suppressions, declared contracts, the opt-in loop
  policy, `audit_diff.py`, the remediation run (`remediation_run/1.2`) and the remediation CLI are unchanged.
  `TOOL_VERSION` is `2.0.0` (manifests updated); the only test touched for that is the version assertion.
- The design engine is a separate package and CLI; the audit and remediation entry points do not import it.

## Tests and validations

- New: `tests/test_design_v20.py` (54 tests: validation positives and negatives with JSON paths, advisor rules
  including microservices only when explicit, blueprint determinism and layouts, generator safety and idempotency,
  pipeline and trust-model denials, the generated tests executed through the trust model when fastapi/httpx/pytest
  are importable, report renderings, snapshot equality, CLI exit codes) and `tests/test_audit_v20.py` (5 tests).
- Fixtures: `tests/fixtures/design20/specs/{valid,invalid}` (6 + 9 contracts) and
  `tests/fixtures/regressions_v20/nested_routers`.
- Full unit suite on Python 3.13 and 3.9, `tests/test_docs_links.py`, `scripts/validate_structure.py`
  (extended with the v2.0 artefacts), `claude plugin validate`, `git diff --check`: see the increment report.

## Known limits (this increment)

- No Alembic environment or migration for PostgreSQL contracts; generated PostgreSQL tests skip without
  `DATABASE_URL` and were not executed against PostgreSQL in this increment (no claim is made).
- No OAuth2/JWT or session authentication, authorization policies, tenant membership binding, outbox/inbox,
  workers, Docker/CI files, end-to-end tests or deployable splitting for `microservices`.
- The auditor matches `public_routes` against router-local paths; prefix composition through `include_router` is a
  pending auditor improvement, and the generated declarations account for it (`/*` when there is no
  authentication).
- The local executor is process hygiene, not a sandbox; generated code runs with the operator's privileges when
  authorised.

## Roadmap for the next v2.0 increments

1. Alembic environment and first migration from the generated models; opt-in PostgreSQL run of a generated project
   under the `tenant-ledger` discipline.
2. Extending an existing project from a contract (read the current layout, add a module, preserve everything).
3. Authentication beyond API keys (OAuth2/JWT with the references' guidance) and tenant membership binding that
   satisfies the auditor's `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` question.
4. Prefix composition in the auditor and full-path `public_routes`.
5. Operations files from `assets/` (Dockerfile, CI, pre-commit) and the reliability patterns (outbox, workers)
   as opt-in generation guarded by decisions in the contract.
