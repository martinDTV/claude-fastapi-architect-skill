# Design engine (v2.0): architecture record

Status: increments 1 to 4 implemented and tested locally; not published.

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

## Increment 4: recoverable writes, changes to existing modules, adoption

**A journal instead of a promise.** The two-phase writer of increment 3 checked every precondition and renamed
files one by one; a crash between two renames left a mixed tree with nothing to say about it. The recoverable
writer (`journal.py`) stages the new content and a backup of every existing file under
`.fastapi-architect/ops/<id>/`, writes a journal (operation id = sha256 of the plan, files with expected and new
digests, state), publishes file by file while recording each landing, and deletes the journal only after a clean
cleanup. `recover` classifies each file by digest (published, pending, conflict), rolls back or rolls forward the
first two kinds, never touches a conflict, journals its own progress and is idempotent. We say what we have: atomic
renames per file, a recoverable protocol across files, no global atomicity. Fault injection points exist for the
tests because a protocol that was never interrupted on purpose has not been tested.

**Changing an existing module is still regeneration.** Contract 1.1 adds `modify_modules`: fields, operations and
dependencies are merged into the baseline contract, the whole contract is validated again, and the module's own
files are regenerated; a manual change on them blocks, as for integration points. Each change is classified and
the potentially incompatible ones (a required field without default, a NOT NULL column without backfill, a NOT
NULL foreign key on an existing table) are refused with the safe form spelled out. Field defaults became part of the
design contract (schema, model and server default) because they are the only honest backfill strategy the
generator can offer.

**Additive column migrations, nothing more.** The migration lineage now carries the baseline contract, so the
delta revision can emit `add_column` for existing tables next to `create_table` for new ones; downgrade drops
them. Verified on a throwaway PostgreSQL: the evolved suite passes, a server default backfills existing rows and
the additive revision rolls back without touching them, and a conflicting column created by hand makes the
upgrade fail with the schema left at the previous revision (transactional DDL) and the verification never
`verified`.

**Adoption is a declaration, not a conversion.** A hand-written project has no manifest, so nothing in it is
managed. `propose` describes candidates; the operator's adoption contract names modules and files; `adopt`
records digests, routers and routes in an adoption baseline and writes nothing else. Evolution of an adopted
module is limited to anchored structural edits (`transforms.py`: a handler appended to the router module, an
optional field appended to a class body, an import after the last import), analysed with `ast`, verified by
comparing the AST before and after (every pre-existing node unchanged, exactly the inserted nodes added), and
refused on ambiguity or duplication. Unadopted files are protected; a manual change on an adopted file blocks
until the baseline is refreshed. The standard library is enough for additive edits; a CST library would only be
justified to rewrite existing nodes, which this increment refuses to do.

**Verified in this increment.** Scenario A: inventory-service gains two fields on Product (verified with its 9
tests through the trust model, applied, no-op on the second run); orders-platform gains two columns on customers
and an update operation on orders, verified on a throwaway PostgreSQL 16 with its evolved suite, plus the
backfill and migration-failure controls. Scenario B: the hand-written shop fixture is adopted module by module,
gains an endpoint and an optional field, passes its own tests through the trust model, and keeps `users.py`
byte-identical; a concurrent manual edit after the plan blocks the write. Fault injection: a publication
interrupted after two of six files is rolled back to the original tree or rolled forward to the complete one; an
interrupted recovery resumes; a file edited after the crash is reported and left alone.

## Increment 5: identity, authorization and tenant isolation

**Five layers, named apart, each built only when buildable.** The temptation in a generator is to accept
`authentication: oauth2_jwt` and emit a plausible stub, or to accept `strategy: shared_schema_rls` and emit a
policy that nobody can run. Schema 1.2 instead makes the author say enough to build each layer: an `oidc` block
for tokens (issuer, audience, asymmetric algorithms, JWKS, claims), a binding for the tenant (a grant or a verified
claim), a policy object for operations and ownership, `owned` on the entities that have an owner, and a strategy
that the persistence can honour. Everything the author does not say enough about is either an error at a JSON
path (RLS on an in-memory store, roles on a shared API key, `HS256`) or a refusal to generate (sessions, a 1.1
`oauth2_jwt` without provider), never a stub. The `guarantees` block, the manifest's `security` record and the
advisor's notes all come from the same normalised contract, so the report cannot claim more than the code.

**Old contracts keep their old meaning.** A 1.0/1.1 document that declared `shared_schema_rls` was told, in
increment 2, that it got application scoping and a pending decision. Under the 1.2 engine it still does: the
default strategy depends on the schema version, `Spec.rls` requires schema 1.2, and the evolution planner names
the declared-but-unimplemented strategy in its risks without upgrading it. `security` and `persistence` are not
evolution operations at all. Moving a running project to RLS is a new design with its own tests; the engine
refuses to do it as a side effect.

**Identity is what the verifier proves, not what the token says.** The generated OIDC module reads nothing but
the header before verifying the signature, checks the algorithm against the contract's list before looking up any
key, requires `iss`/`aud`/`exp`/`nbf`/`iat`/subject, and builds the principal from the verified claims only. Key
management is deliberately conservative: https only, a cache bounded by the contract, one refresh per unknown key
id and never more often than every 30 s, a static JWKS only in the test environment, and 503 rather than 401 when
the provider cannot be reached, so an outage is never mistaken for an attack. The generated negative tests
include the classic confusions (HS256 with the public key, `alg: none`, a rotated key under the same `kid`,
altered claims with the original signature). PyJWT with cryptography is the only new dependency; the project
issues nothing and stores no session.

**Authorization lives where the data is.** Per-operation roles are a route dependency because they need nothing
but the principal; ownership is checked in the service against the row it loaded and, for `owner` visibility, in
the repository query itself, because a 404 that comes from the query cannot be bypassed by a new route. The router
only carries the principal. The three answers are kept apart on purpose: 403 when the row was visible and the
caller may not change it, 404 when the row is not the caller's to see (another tenant, another owner, or nothing
there), 503 when the infrastructure failed. The auditor sees the dependency and the principal flowing with the
resource id on every generated route; the stripped-route negative control is caught through the declared contract
the generator emits (`mutation_authorization_required`), which is more honest than a regex on names.

**Row-level security the tenant-ledger way, automated.** The policy is created in the same revision as its table,
so no table ever exists unprotected; `FORCE` makes the owner subject to it as well; the application role gets row
privileges and nothing else, and the migration checks on the live connection that it cannot bypass RLS before
granting anything (transactional DDL leaves nothing behind when it refuses). The context is set with
`set_config(..., true)` from the authenticated principal inside the request transaction, never from the request
and never at session level, so commit, rollback and the pool reset it. The generated `tests/test_rls.py` is the
adversarial list of `references/authz-multitenancy.md` run through the restricted role, including the pooled
connection, the rollback residue, the invalid context, the role trying to lift the protection and two tenants
interleaved on one process. Roles are provisioning: the migration never creates one with a default password.

**Verified in this increment.** `team-notes` (OIDC, claim binding, roles and ownership) passes 54 generated tests
through the trust model and audits clean; the owner-scoped single-tenant fixture passes too. `tenant-projects`
passes 51 generated tests on a throwaway PostgreSQL 16 as the restricted role, a `BYPASSRLS` or missing role makes
the migration refuse with no table behind, and the evolution that adds a module protects its table and passes 64
tests. The auditor's rules and regexes did not change; the corpora were not re-scored because nothing that scores
them moved. Evidence: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.
