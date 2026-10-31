# Design engine (v2.0): architecture record

Status: increments 1, 2 and 3 implemented and tested locally; not published.

## Decision

FastAPI Architect v2.0 adds a **design engine** next to the audit (v1.3–v1.8) and remediation (v1.9) engines,
without rewriting either. The engine is a pipeline of pure, deterministic steps with one impure step (writing
files) guarded by explicit flags, and one executing step (running generated tests) guarded by the v1.9 trust
model:

```
spec.json ──validate──▶ Spec (provenance, digest)
                 ──recommend──▶ Recommendation (style, rejected, assumptions, pending)
                 ──build──▶ Blueprint {path: content}        pure, sorted, no timestamps
                 ──generate──▶ target directory              idempotent, never overwrites without --overwrite
                 ──audit_generated──▶ audit report           static, reuses fastapi_audit.engine
                 ──run_generated_tests──▶ test evidence      only through select_executor + runner adapters
                 ──report──▶ architecture_report/1.0
```

Package: `plugins/fastapi-architect/skills/fastapi-architect/scripts/fastapi_design/` (`spec.py`, `advisor.py`,
`blueprint.py`, `generator.py`, `verify.py`, `report.py`, `pipeline.py`); CLI `design_fastapi.py` in the skill
with a root shim in `scripts/`; contract documented in `references/design-contracts.md`.

## Why a declarative contract first

The engine must separate three things the author needs to see apart: what was **required**, what was
**assumed**, and what is **pending**. A JSON contract with strict validation and provenance gives that for free:
every default is recorded as an assumption, every `status: pending` decision and open question is carried into
the report, and the digest ties the generated project to the exact contract. The contract is data, never
executed, and validated fail-closed like `.audit-fastapi.json` (same style, same error reporting with JSON paths).

## Why the advisor is rule-based and conservative

The brief forbids imposing DDD, microservices, CQRS or hexagonal architecture by default. The advisor therefore
starts from the cheapest structure (layered) and only moves up when the contract shows the cost is paid for:
module count, scope and team size for the modular monolith; outbound integrations, criticality or contract tests
for ports and adapters; stated independent deployment plus team/module justification for microservices. Each
rejected alternative is reported with its reason, so the author can overrule with `style_preference` (honoured,
with a warning). Signals that hint at CQRS/eventing (eventual consistency, a broker, auditability) become pending
decisions with pointers to the references, not structure.

## Why generation is a blueprint plus a guarded writer

Keeping the blueprint pure makes determinism and idempotency testable (two builds compare equal; a committed
snapshot equals a fresh generation). The writer owns every safety rule: empty target or `--allow-existing`,
conflicts never replaced without `--overwrite`, no deletion, atomic writes, path safety, manifest with file
digests and no timestamps. Existing projects can therefore be extended module by module without risk.

## Why the auditor had to learn one more fact

The generated projects mount every business router into `api_router = APIRouter(prefix="/api/v1",
dependencies=[Depends(require_api_key)])`. FastAPI applies those dependencies to every included router, but the
auditor only recognised the router that carried them, so the generated code was reported as unauthenticated.
Rather than bend the generated code towards the auditor, the facts pass now records `X.include_router(Y)` edges
and propagates protection to a fixpoint (any file order, any depth, names expanded to every import form). The
change is covered by a regression fixture with a negative control, and the dev/acceptance corpora and the two
calibration projects score identically before and after (per-rule tp/fp/fn/tn unchanged).

## Trust model for generated code

Generated code is the tool's own output, but it is still code: it runs only when the operator passes
`--authorize-execution` with a trust profile the executor accepts (`fixture` or `trusted`; `untrusted` fails
closed), through the same `LocalProcessExecutor` and runner adapters as v1.9 (process hygiene, not a sandbox;
structured evidence bound to the run). The generated project is used as the workspace project directly instead
of being copied: it is not a foreign project, and its digest is compared before and after the run.

## Verified in this increment

- 48 example and fixture contracts validated (6 valid fixtures, 9 invalid fixtures with their expected JSON
  paths, 2 example contracts); every generatable contract produces a project whose Python parses.
- `inventory-service` (layered, in-memory, API key): generated, audited clean (0 findings, 5 authenticated
  routes, 1 public), its 9 generated tests passed through the trust model with pytest-junit evidence; the
  generated snapshot is committed under `plugins/fastapi-architect/examples/generated/inventory-service` and a
  test asserts it equals a fresh generation.
- `orders-platform` (hexagonal, PostgreSQL, multi-tenant, two integrations): generated into a temporary
  directory, audited with one informational finding (import-linter present); its tests are generated but skip
  without `DATABASE_URL` and are not executed by the suite (no PostgreSQL claim is made for generated code).

## Limits and next increments

- Alembic environment and first migration for PostgreSQL contracts; a PostgreSQL run of a generated project under
  the same opt-in discipline as `tenant-ledger`.
- OAuth2/JWT and session authentication, authorization policies, tenant membership binding (the auditor's
  `authorization_unknown` contracts and the advisor's pending decisions point at them).
- Prefix composition through `include_router` in the auditor, so `public_routes` can be declared with full paths.
- Extending an existing (non-generated) project from a contract; the writer already supports adding files to a
  non-empty target, the blueprint does not yet read an existing layout.
- Docker, CI and pre-commit files from `assets/`, workers and outbox from the references.

## Increment 2: persistence and contract hardening

**PostgreSQL is a real target, not a stub.** A PostgreSQL contract now gets a declarative base with a naming
convention, an Alembic environment and an initial migration generated from the contract (revision id derived from
the contract digest), composite foreign keys between tenant tables, a request-scoped session that rolls back on
errors, services that own the unit of work (flush + commit) and turn constraint violations into 409, and a test
suite that only accepts an empty database, migrates it, truncates between tests, checks `compare_metadata` and
repeats up/down. The decision to keep flushes out of repositories came from the first PostgreSQL run: an
`IntegrityError` raised by a flush inside `add()` escaped the service's handler and surfaced as a 500.

**The contract says what the generator cannot guarantee.** Schema 1.1 adds `references`/`on_delete` (foreign
keys that must follow the module dependency direction) and `security.multi_tenant.binding`. Only
`api_key_registry` is implemented (each key bound to one tenant, a dependency resolves the tenant path parameter
and refuses others with 403). A multi-tenant contract without a binding validates but is refused for generation:
a route that accepts any `tenant_id` from any authenticated caller is not authorization. The report's
`guarantees` block separates requested design, declared strategy, implemented strategy
(`shared_schema_application_scoping`), authentication, principal binding, resource authorization (not
generated) and storage isolation, and lists what is not claimed (RLS, schema/database per tenant, resource
authorization, OAuth2/sessions, network or filesystem isolation of executed tests).

**The auditor composes routes.** Route contracts carry `effective_paths` and `path_resolution` (resolved,
ambiguous, unresolved, unmounted) composed through `APIRouter(prefix=...)`, `include_router(..., prefix=...)` and
applications created in factories. `public_routes` and `route_auth_required` match effective paths as well as
router-local ones; a router mounted twice is public only if every mount is declared; a non-constant prefix yields
`unresolved` and nothing is invented. Fingerprints are untouched because messages keep the handler's own path.
The generated projects therefore declare full public paths again (`/health`, `/api/v1/*`). Corpora and the two
calibration projects score identically before and after.

**Verification runs in a copy.** The first increment used the generated target as the workspace project; that
contradicted the executor's own guarantee ("runs in a copy of the project") and would have let `uv run` write a
virtual environment and a lock file into the committed snapshot. `run_generated_tests` now copies the target
(`Workspace.create`) and reports the copy's Python digest before and after. The opt-in PostgreSQL test
(`FA_DESIGN_PG=1`) creates a throwaway cluster, runs the generated suite through the trust model with
`DATABASE_URL`, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` passed through, and includes a negative control that
mutates a model and expects the drift test to fail. Evidence: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.

## Increment 3: safe project evolution

**The manifest is the contract of ownership.** Since 1.1 the generation manifest embeds the architecture
contract, the migration chain (revision, parent, file, tables) and the operation history, next to the sha256 of
every generated file. Discovery derives ownership from it and from nothing else: a file is managed only if the
manifest lists it; a listed file whose digest differs is a manual change; an unlisted file is the developer's,
whatever its name. A project without a manifest has `unknown` ownership everywhere and is refused by the
evolution engine, although discovery still describes it.

**Evolution is regeneration from a merged contract, never text surgery.** The new modules are appended to the
baseline contract, the whole contract is validated again (cycles, references, duplicates, reserved names) and two
blueprints are built: what every managed file should contain now and what it should become. The difference is
the plan. Files whose generated content does not change are never written, so manual changes on them survive;
integration points that must change are regenerated only when they are managed, unchanged, allowed and not
preserved; everything else is a conflict that blocks. Historical migrations are kept verbatim and one additive
revision is chained after the recorded head. The layout is pinned: the advisor's new opinion is reported, not
applied.

**Writes are two-phase and re-check the world.** `write_files` verifies every expected digest (or absence)
immediately before writing, writes temporaries, then renames; a precondition failure or an I/O error before the
renames leaves the project untouched. Symlinked projects and writes through symlinks are refused.

**Verification happens on a copy first.** `apply` applies the plan to a copy, audits it against the project by
fingerprint, checks that existing routes keep their paths and authentication, checks that nothing outside the
plan moved, runs the tests through the trust model (the operator's scope covers the project and is moved to the
copy), and only then writes the project. `verified` needs executed tests; `audit_clean` is the ceiling without
them; `inconclusive` and `rejected` never write.

**Verified in this increment.** `inventory-service` + `suppliers`: 18 tests through the trust model, applied,
no-op on the second run, a manual change on `services/catalog.py` preserved by a third evolution, a hand-edited
`api/router.py` blocking a fourth. `orders-platform` + `shipments`: additive migration chained after the initial
revision, 50 generated tests on a throwaway PostgreSQL 16 through the trust model, database left with
`alembic_version` only; the historical migration byte-identical. Evidence in
`plugins/fastapi-architect/examples/generated/VERIFICATION.md`.
