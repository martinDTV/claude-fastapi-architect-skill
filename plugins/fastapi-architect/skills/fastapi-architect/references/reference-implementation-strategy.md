# Reference implementation strategy

## Trigger
The user asks for a complete runnable example, a generated project, a "working" template, or validation that generated code actually runs. Also use it when deciding whether a snippet may be presented as executable.

## Status in this repository
Since v1.5 the plugin ships one reference implementation labelled **`verified`**: `plugins/fastapi-architect/examples/tenant-ledger` (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL with row-level security; authorization per resource, atomic and optimistic concurrency, idempotency keys, one commit per use case, migrations from empty and from the previous revision, two anti-patterns reproduced next to their fixes). Its `VERIFICATION.md` records versions, commands and results; its `LIMITATIONS.md` lists what it does not cover; its 27 tests start a throwaway PostgreSQL cluster, and since v1.6 `tests/test_adversarial.py` reproduces next to their fixes the scenarios the auditor can only suspect: a shared session, a request-scoped session handed to a task, cancellation by timeout, a lost response retried with and without an idempotency key, and the RLS configuration invariants. Point users to it for the decisions it demonstrates and keep its label honest: after any dependency upgrade it is `draft` until `VERIFICATION.md` is renewed.

Everything else in the references remains guidance and snippets, not validated projects. This document defines how further examples must be built, labelled and verified before being offered as proven.

## Principles
1. **Executable or labelled.** Code is either runnable with declared versions and recorded verification, or it is explicitly marked as illustrative. There is no third category.
2. **One decision per example.** Each example exists to demonstrate one architectural decision and the tests that prove it, with the smallest complete surface around it.
3. **Reproducible.** Pinned dependencies, a lockfile, containerised infrastructure, deterministic tests, no network calls to third parties.
4. **Negative tests are mandatory.** An example that only shows the happy path proves nothing about the decision it claims to demonstrate.
5. **Evidence is recorded.** What was run, with which versions, on which date, with which result. Claims without a recorded run are not claims.

## Verification labels
Every example, snippet or generated project carries exactly one label, stated where the user will see it:

| Label | Meaning | Allowed wording |
|---|---|---|
| `verified` | Full toolchain and tests ran in a recorded environment; `VERIFICATION.md` exists | "Runs and passes its tests under the recorded versions." |
| `draft` | Runs, but tests are incomplete or the verification record is missing | "Runs; verification incomplete, see limitations." |
| `illustrative` | Documentation or pseudocode; never executed as a whole | "Illustrates the shape; not a validated implementation." |

Downgrade the label when dependencies move: a `verified` example whose lockfile is older than the current major versions of FastAPI, Starlette, Pydantic or SQLAlchemy becomes `draft` until re-verified.

## Catalogue of reference implementations

| Example | Decision it demonstrates | Must prove (positive) | Must prove (negative) | Infrastructure |
|---|---|---|---|---|
| Small CRUD | Stage 0/1 right-sizing: no repository or ports where not needed | CRUD endpoints with schemas, pagination, Problem Details | Validation errors, 404 mapping, no ORM object leaks | SQLite or PostgreSQL container |
| Modular monolith | Modules by capability, `public.py` facades, import-linter contracts, service-owned transactions | Two modules collaborating through the facade; contracts pass | Contract fails on a direct cross-module import; rollback on service error | PostgreSQL container |
| DDD / hexagonal module | Pure domain, `Protocol` ports, adapters, fake for tests, applied to one module only | Domain invariants unit-tested without I/O; adapter swap by `Depends` | Invariant violations rejected before persistence; fake and real adapter pass the same contract tests | PostgreSQL container, provider fake |
| Multi-tenant SaaS with PostgreSQL RLS | Transaction-local tenant context, non-bypass role, fail closed | Tenant A reads only A; context set per transaction | Cross-tenant read, write, join, aggregate, raw SQL and background job all fail closed; missing context yields no rows | PostgreSQL with RLS policies and two roles |
| Optimistic concurrency with `If-Match` | Version column, ETag, atomic compare-and-swap | Update with matching ETag succeeds and bumps the version | Two concurrent updates with the same ETag: exactly one succeeds, the other gets 412; missing `If-Match` handled per documented policy | PostgreSQL container |
| OAuth / OIDC / JWKS | Token validation against a key set with rotation | Valid token accepted; `kid` lookup; claims mapped to identity | Expired, wrong issuer, wrong audience, wrong algorithm, removed key, unsigned token all rejected; JWKS refresh limits | Local JWKS server or fixture keys |
| Workers with outbox / inbox | At-least-once delivery, idempotent consumers, leases | Event written in the same transaction as the state change; relay delivers | Crash after commit before publish still delivers; duplicate delivery has one effect; stale lease is recovered | PostgreSQL container, worker process |
| Idempotent operations | Idempotency key storage and replay semantics | Same key returns the first result | Same key with a different payload rejected; concurrent duplicates yield one side effect; key expiry documented | PostgreSQL container |
| Alembic migrations | Naming convention, autogenerate hygiene, up and down | `upgrade head` on empty DB; `alembic check` clean | Downgrade restores previous schema; drift between models and migrations fails CI | PostgreSQL container |
| Real PostgreSQL tests | Per-test rollback, fixtures, parallel safety | Suite passes against PostgreSQL in CI | Unique constraint, FK and RLS failures observed, not mocked | PostgreSQL container |
| OpenTelemetry instrumentation | Traces across HTTP, DB and worker; request IDs | Spans emitted with correct parentage; no PII in attributes | Missing exporter does not break requests; sampling respected | OTLP collector container |
| Container deployment | Multi-stage image, non-root, health checks, graceful shutdown | Image builds; health endpoint; shutdown drains in-flight requests | Container fails fast on missing config; no secrets in the image | Docker |

Build them in roughly this order; later examples reuse the earlier ones. Keep each example independent enough to run alone.

## Required contents of every example

```
examples/<name>/
├── README.md            # purpose, decision demonstrated, how to run, how to test, label
├── pyproject.toml       # pinned ranges
├── uv.lock              # exact versions used for verification
├── src/app/...          # the application
├── tests/               # positive and negative tests; markers for DB-backed tests
├── compose.yaml         # PostgreSQL / collector / JWKS fixtures as needed
├── alembic/             # when the example has a schema
├── VERIFICATION.md      # recorded runs: commands, versions, date, environment, result
└── LIMITATIONS.md       # known gaps, unsupported scenarios, security expectations not covered
```

Checklist that must be true before the label `verified` is applied:

- [ ] Versions and dependencies declared in `pyproject.toml` and frozen in `uv.lock`; Python version stated.
- [ ] Run instructions work from a clean clone: install, start infrastructure, migrate, run, test, stop.
- [ ] Positive tests cover the decision's happy path end to end through HTTP.
- [ ] Negative tests cover the failure conditions listed in the catalogue row.
- [ ] Failure conditions documented: what happens when the DB is down, the token is invalid, the provider times out, the config is missing.
- [ ] Security expectations stated and tested: authorization fail-closed, no secrets in code or logs, tenant isolation where applicable, dependency audit run.
- [ ] Known limitations written down, including what the example deliberately does not handle.
- [ ] `VERIFICATION.md` records `ruff format --check`, `ruff check`, `mypy --strict`, `lint-imports` (if boundaries exist), `pytest` against the real infrastructure, migration up/down, container build, with versions and date.
- [ ] No network access to third-party services during tests; providers are fakes with contract tests.

## Verification protocol

1. Start from a clean environment (fresh virtualenv or container). Install from the lockfile.
2. Start infrastructure with `compose.yaml`; wait for health.
3. Run, in order: format check, lint, strict type check, import boundaries, migrations up, tests, migrations down, image build. Stop at the first failure.
4. Record each command, its exit status and the versions of Python, FastAPI, Starlette, Pydantic, SQLAlchemy, driver and database in `VERIFICATION.md`, with the date.
5. Re-run the protocol when a dependency is upgraded or the example changes. Replace the record; keep the previous one in version control history.

If any step cannot be executed in the current environment (no Docker, no network for package install), stop and label the example `draft` with the missing steps listed. Do not describe steps that did not run.

## Generating a project for a user

When asked to generate a runnable project:

1. State the stage and the decisions in two or three lines, then produce the complete project: no `...`, no "left as an exercise", no imports of modules that do not exist.
2. Run the project's toolchain and tests yourself with the project's commands. Report exactly which commands ran and their results.
3. If a tool is unavailable, say which, what it would have checked, and label the output `draft`.
4. Include negative tests for the decision the user cares about (authorization, concurrency, idempotency) even when not asked; they are what make the example worth copying.
5. Point to the matching catalogue row and its failure conditions so the user knows what is and is not covered.

## Security expectations common to all examples

Secrets via environment or secret manager, never committed; `SecretStr` in settings. Authorization enforced in services or dependencies, tested with negative cases. Tenant scope on every query path in multi-tenant examples. No PII or tokens in logs, traces or error bodies. Dependency audit (`pip-audit` or equivalent) recorded in verification. Containers run as non-root with pinned base images.

## Anti-patterns to reject

- Presenting a reference document or a snippet as "a working example" without a recorded run.
- Claiming tests passed when they were not executed, or executed against mocks when the decision needs the real database.
- Pinning versions from memory instead of resolving them with the package manager.
- Mocking the database in tests that exist to prove transactions, constraints, RLS or concurrency.
- Examples whose negative tests are commented out, skipped by default or absent.
- Copying the full modular-monolith layout into a four-route CRUD example; the example must match its catalogue row's stage.

## Relationship to other references

Structure and standards come from `references/project-structure.md` and `references/code-standards.md`; test design from `references/testing.md` and `references/testing-matrix.md`; tenancy and identity from `references/authz-multitenancy.md` and `references/identity-threat-models.md`; outbox and workers from `references/events-and-workflows.md`; migrations from `references/data-layer.md` and `references/database-evolution.md`; containers and telemetry from `references/operations.md` and `references/operations-production.md`. Audits of generated examples follow `references/architecture-audit.md`.
