# Verification of the generated `orders-platform` backend on PostgreSQL

Status: **verified** for what is listed below, on 2026-10-30, on the developer's machine, through the v1.9 trust
model (process hygiene, not a sandbox). Nothing here is a claim about environments that were not exercised.

## What ran

`FA_DESIGN_PG=1 FA_DESIGN_PG_EVIDENCE=<dir> python3 -m unittest tests.test_design_v20_postgresql -v`
(`tests/test_design_v20_postgresql.py`, cluster lifecycle in `tests/_pg_support.py`):

1. a throwaway PostgreSQL cluster (`initdb -A trust --no-sync`, `pg_ctl start` on 127.0.0.1 and a free port,
   `fsync=off`, `synchronous_commit=off`) under a temporary directory, deleted afterwards;
2. an empty database (`information_schema.tables` in `public`: 0 rows, asserted before the run);
3. `orders-platform` generated from `../specs/orders-platform.json` (contract digest `f16d2acd4f57…`, 53 files,
   manifest `fastapi-architect/generation/1.0`) into a temporary target, audited statically (1 informational
   finding, `ARCH_IMPORT_LINTER_PRESENT`; route contracts: 12 routes, 11 authenticated with visible membership
   authorization, 1 public, 0 unauthenticated-not-public);
4. the generated suite executed by `run_generated_tests` with `Authorization(execution=True, trust="fixture",
   scope=<target>)`, `LocalProcessExecutor` (`isolation_level: process_hygiene`), in a **copy** of the target
   (`Workspace.create`; 45 Python files digested, none changed by the run), command
   `uv run --group dev pytest -q -p no:cacheprovider` instrumented by the pytest JUnit adapter
   (`--junit-xml=<evidence>/pytest-<nonce>.xml -o junit_family=xunit2 -o junit_suite_name=<nonce>`), with
   `DATABASE_URL`, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` passed through (uv synced 42 packages from the local
   cache into the copy's virtual environment);
5. a negative control: a second generation whose `customers` model received an extra column, running only
   `tests/test_migrations.py -k matches_the_models` under the same authorization.

## Evidence

| Item | Value |
|---|---|
| Outcome | `passed` (runner `pytest-junit`, nonce `d68d3df275cf43a5a05cbab17c025f27` matched, verifiable) |
| Counts | selected 39, executed 39, passed 39, failed 0, errors 0, skipped 0, collection errors 0 |
| pytest summary | `39 passed, 2 warnings in 5.89s` (the warnings are Starlette's `httpx` deprecation notice) |
| Copy digest | 45 Python files, `python_files_changed_by_the_run: []` |
| Database after the run | `alembic_version` only (the generated `migrated` fixture downgrades to base at the end) |
| Negative control | `failed`: 1 selected, 1 failed (`test_migrated_schema_matches_the_models`), diff names `add_column … customers` |
| Whole opt-in test module (cluster, two generations, two runs) | 13.5 s wall clock, 125 MB peak RSS (`/usr/bin/time -l`) |
| Generation only (`--target … --no-audit`) | 0.07 s, 27 MB |
| Static audit of the generated project | 0.20 s, 26 MB |
| Generation + audit, second run on the same target (`--allow-existing`, nothing rewritten) | 0.22 s, 30 MB |

Passed tests (from the JUnit report, not from the console):

```
tests.test_migrations::test_the_script_directory_has_one_head
tests.test_migrations::test_migrated_schema_matches_the_models          # compare_metadata == [] : no schema drift
tests.test_migrations::test_tables_and_constraints_exist                # tables, (tenant_id, id) uniques, named foreign keys
tests.test_migrations::test_upgrade_and_downgrade_are_repeatable        # downgrade base -> upgrade head -> alembic current == revision
tests.test_customers::test_create_customer, _rejects_invalid_payload, _rejects_unknown_properties,
    test_get_customer_returns_the_created_one_and_404_for_unknown, test_list_customers_paginates,
    test_update_customer, test_delete_customer, test_customer_routes_require_the_api_key,
    test_customer_routes_refuse_another_tenant_for_the_same_key, test_customers_of_one_tenant_are_invisible_to_another,
    test_health_is_public
tests.test_orders::test_create_order, _rejects_invalid_payload, _rejects_unknown_properties,
    test_create_order_with_unknown_customer_id_is_a_conflict_and_leaves_no_row,   # 409 problem+json, list stays empty (rollback)
    test_create_order_cannot_reference_a_customer_id_of_another_tenant,           # 409: composite foreign key (tenant_id, id)
    test_get_order_returns_the_created_one_and_404_for_unknown, test_list_orders_paginates,
    test_order_routes_require_the_api_key, test_order_routes_refuse_another_tenant_for_the_same_key,
    test_orders_of_one_tenant_are_invisible_to_another, test_create_order_notifies_the_payments_port, test_health_is_public
tests.test_billing::test_create_invoice, _rejects_invalid_payload, _rejects_unknown_properties,
    test_create_invoice_with_unknown_order_id_is_a_conflict_and_leaves_no_row,
    test_create_invoice_cannot_reference_a_order_id_of_another_tenant,
    test_get_invoice_returns_the_created_one_and_404_for_unknown, test_list_invoices_paginates,
    test_invoice_routes_require_the_api_key, test_invoice_routes_refuse_another_tenant_for_the_same_key,
    test_invoices_of_one_tenant_are_invisible_to_another, test_create_invoice_notifies_the_notifications_port,
    test_health_is_public
```

What each requirement of the increment maps to: empty database (step 2 and the `migrated` fixture, which fails on
a non-empty one); migrations applied (`alembic upgrade head` in `migrated`); tables and constraints
(`test_tables_and_constraints_exist`); insert and read through the API (`test_create_*`, `test_get_*`,
`test_list_*`); rollback (`*_is_a_conflict_and_leaves_no_row`: the 409 leaves the listing empty); referential
integrity (`*_is_a_conflict_*` and `*_cannot_reference_*_of_another_tenant`, the latter through the composite
foreign key); repeatable execution (`test_upgrade_and_downgrade_are_repeatable`, plus the per-test truncation);
detection of unexpected schema changes (`test_migrated_schema_matches_the_models`, positive in the run and negative
in the control).

## Versions

| Component | Version |
|---|---|
| PostgreSQL | 16.10 (Homebrew), throwaway cluster |
| Python (generated project's environment, uv-managed) | 3.13.5 |
| SQLAlchemy / asyncpg / Alembic | 2.0.55 / 0.30.0 / 1.20.1 |
| FastAPI / Starlette / pydantic / pydantic-settings | 0.162.0 / 1.10.0 / 2.14.2 / 2.16.2 |
| httpx / pytest | 0.28.1 / 9.2.0 |
| uv | 0.11.28 |
| FastAPI Architect | 2.0.0 (local, unpublished), audit schema 1.7, contract schema 1.1 |

## What this does not show

- No isolation beyond process hygiene: the generated tests ran with the operator's privileges, with network access
  (uv resolved from the local cache, but nothing prevented it from reaching the index).
- Row-level security, schema or database per tenant and resource-level authorization are not generated and were
  not tested; cross-tenant protection shown here is the API-key binding (403), query scoping (empty listings) and
  the composite foreign key (409).
- Only `orders-platform` was executed against PostgreSQL. `postgres_layered.json` and other PostgreSQL contracts
  are generated and linted by the suite but their tests were not run.
- The run is reproducible with the command above on a machine with the PostgreSQL binaries and uv; the numbers
  (time, memory) are from one run on an Apple Silicon laptop and are indicative only.

# Increment 3: evolution of the generated examples

Status: **verified** for what is listed below, on 2026-10-30, on the developer's machine, through the v1.9 trust
model (process hygiene, not a sandbox). The examples committed here are the baselines; the evolutions ran on
temporary copies (`tests/test_evolve_v20.py`, `tests/test_design_v20_postgresql.py` with `FA_DESIGN_PG=1`).

## `inventory-service` + `suppliers` (`../evolutions/inventory-add-suppliers.json`)

| Item | Value |
|---|---|
| Plan | create 7 (`api/suppliers.py`, `dependencies/suppliers.py`, `models/suppliers.py`, `repositories/suppliers.py`, `schemas/suppliers.py`, `services/suppliers.py`, `tests/test_suppliers.py`), modify 4 (`api/router.py`, `main.py`, `README.md`, manifest), unchanged 24, conflicts 0 |
| Public contracts | +5 routes (`/api/v1/suppliers`…), 0 removed, declared contracts unchanged |
| Verification in the copy | audit 0 → 0 findings; routes 6 → 11, none missing or changed; 0 files touched outside the plan |
| Tests (pytest-junit, nonce matched) | selected 18, executed 18, passed 18, failed 0, skipped 0 (9 `catalog` + 9 `suppliers`) |
| Applied | 11 files; manifest history `generate`, `evolve` (`suppliers`); discovery afterwards: 34 managed files, all unchanged, modules `catalog, suppliers` |
| Second run | `noop` (every requested module exists with the same definition), nothing written |
| Manual change on `services/catalog.py` | preserved by a third evolution (`returns`), reported as `preserved manual changes`; discovery reports it `managed_modified` |
| Manual change on `api/router.py` | fourth evolution `blocked`: `manual_change_on_integration_point` |
| Hand-written project (`tests/fixtures/regressions_v20/nested_routers`) | discovered (routes, routers), evolution `blocked`: `generation_manifest_present` |
| ruff on the evolved project | all checks passed |

## `orders-platform` + `shipments` (`../evolutions/orders-add-shipments.json`), PostgreSQL

| Item | Value |
|---|---|
| Plan | create 9 (module `shipments`, its tests, `migrations/versions/77d681cb65a8_add_shipments.py`), modify 7 (`.audit-fastapi.json`, manifest, `README.md`, `migrations/env.py`, `pyproject.toml`, `api/router.py`, `tests/test_migrations.py`), kept 1 (`migrations/versions/9dab920675b3_initial_schema.py`, byte-identical) |
| Migration | additive: `create_table shipments` with `UniqueConstraint(tenant_id, id)` and the composite foreign key `(tenant_id, order_id) → orders(tenant_id, id)` `ondelete RESTRICT`, two indexes; `down_revision = 9dab920675b3`; no drop, no alter |
| Verification in the copy | audit 1 → 1 (informational import-linter signal), 0 blocking; routes 12 → 15, none missing or changed; 0 files touched outside the plan |
| Tests on a throwaway PostgreSQL 16 (pytest-junit, nonce `56ffe2fa…` matched) | selected 50, executed 50, passed 50, failed 0, skipped 0 (`50 passed, 2 warnings in 6.61s`); including `test_upgrade_and_downgrade_are_repeatable` over the two-revision chain, `test_migrated_schema_matches_the_models`, `test_create_shipment_with_unknown_order_id_is_a_conflict_and_leaves_no_row`, `test_create_shipment_cannot_reference_a_order_id_of_another_tenant` |
| Applied | 16 files; discovery afterwards: 2 revisions, head `77d681cb65a8`, historical modified none, modules `billing, customers, orders, shipments` |
| Database after the run | `alembic_version` only |
| Whole opt-in module (two clusters, three runs) | 24.4 s wall clock, 123 MB peak RSS |

## Cost of the evolution steps (`/usr/bin/time -l`, one run each, indicative)

| Step | inventory-service | orders-platform |
|---|---|---|
| `discover` | 0.13 s, 31 MB | 0.28 s, 37 MB |
| `plan --no-verify` | 0.13 s, 33 MB | 0.28 s, 36 MB |
| `plan` (verification copy, audit, invariants, no tests) | 0.24 s, 32 MB | 0.65 s, 37 MB |
| `apply` with tests through the trust model | 0.89 s, 82 MB (18 tests, in-process interpreter) | 24.4 s, 123 MB for the whole opt-in module (two clusters, `uv run`, three suites) |

Versions as in the increment 2 table above (PostgreSQL 16.10, SQLAlchemy 2.0.55, asyncpg 0.30.0, Alembic 1.20.1,
FastAPI 0.162.0, uv 0.11.28).

## What this does not show

- Evolution of hand-written projects, changes to existing modules, layout migrations and merges of Alembic heads
  are refused by design, not tested as working.
- The verification copy runs with the operator's privileges and network access; `verified` means the generated
  suite passed there, nothing more.

# Increment 4: changes to existing modules, adoption, recoverable writes

Status: **verified** for what is listed below, on 2026-10-30, on the developer's machine, through the v1.9 trust
model (process hygiene, not a sandbox). Evolutions ran on temporary copies (`tests/test_evolve_v20_adoption.py`,
`tests/test_evolve_v20_recovery.py`, `tests/test_design_v20_postgresql.py` with `FA_DESIGN_PG=1`).

## Scenario A: `orders-platform` + `../evolutions/orders-add-customer-columns.json`, PostgreSQL

| Item | Value |
|---|---|
| Changes | `customers.Customer.phone` compatible (nullable column); `customers.Customer.tier` compatible (NOT NULL with server default `1`); `orders.Order:update` compatible (new use case, route, tests) |
| Plan | create 1 (`migrations/versions/e2dc0f95b7da_add_customers_phone_customers_tier.py`), modify 12 (customers and orders module files, `tests/test_customers.py`, `tests/test_orders.py`, `tests/test_migrations.py`, `README.md`, manifest), kept 1 (initial revision, byte-identical) |
| Migration | `down_revision = 9dab920675b3`; `add_column phone nullable`, `add_column tier NOT NULL server_default 1`; downgrade drops both; no alter, no drop of existing objects |
| Verification in the copy | audit 1 → 1 (informational), 0 blocking; routes 12 → 13 (`PATCH /tenants/{tenant_id}/orders/{order_id}` added), none missing or changed; 0 files touched outside the plan |
| Tests on a throwaway PostgreSQL 16 (pytest-junit, nonce matched) | selected 40, executed 40, passed 40, failed 0, skipped 0 (`40 passed, 2 warnings in 6.07s`), including `test_update_order`, `test_migrated_schema_matches_the_models`, `test_upgrade_and_downgrade_are_repeatable` over the two-revision chain |
| Applied | 13 files through the recoverable writer; discovery afterwards: 2 revisions, historical modified none |
| Backfill control | initial revision applied, one customer inserted by hand, the evolved project's `alembic upgrade head` succeeds: `tier = 1`, `phone IS NULL`, `tier` NOT NULL; `alembic downgrade -1` removes both columns and the row survives |
| Migration-failure control | a `tier text` column created by hand before the upgrade: `alembic upgrade head` fails (`DuplicateColumnError`), `alembic_version` stays at `9dab920675b3`, `phone` was not added (transactional DDL); the verification afterwards ends `noop`/`rejected`, never `verified` |
| NOT NULL without backfill | `customers.Customer.vat` (required, no default) → `potentially_incompatible`, plan `blocked` (`backfill_strategy`) |
| Whole opt-in module (increments 2 to 4, three clusters, six tests) | 40.9 s wall clock, 125 MB peak RSS |

## Scenario A on the in-memory example: `inventory-service` + `../evolutions/inventory-add-product-fields.json`

Plan: modify 6 (`models/catalog.py`, `schemas/catalog.py`, `services/catalog.py`, `tests/test_catalog.py`,
`README.md`, manifest), create 0, unchanged 22; verification in the copy: audit 0 → 0, routes 6 → 6, 0 files
touched outside the plan; 9 tests passed through the trust model; applied; second run `noop`; ruff clean.

## Scenario B: `tests/fixtures/adoption20/handmade_shop` (hand-written)

| Item | Value |
|---|---|
| `propose` | candidates `items` (router in `app/routers/items.py`, mounted at `/api/items`, 3 routes, related `app/schemas.py`) and `users`; 7 unassigned python files; nothing adopted |
| `adopt` (`adopt-items.json`) | `.fastapi-architect/adoption.json` written and nothing else; ownership afterwards: 2 `adopted_unchanged`, 8 `unadopted` |
| Evolution (`items-add-stats.json`) | `GET /stats` handler appended to `items.py`, `ItemIn.sku: str | None = None` appended to `schemas.py`; AST evidence: every pre-existing top-level node unchanged, one node added per file; comments (`# explicit 404`, `# sync on purpose`) and spacing byte-identical |
| Verification in the copy | audit 1 → 1, 0 blocking; routes 6 → 7 (`GET /stats` added), none missing; 0 files touched outside the plan; the project's own 3 tests passed through the trust model |
| Applied | `items.py`, `schemas.py`, `adoption.json`; `users.py` byte-identical |
| Controls | unadopted `users` → `blocked` (`module_is_adopted`); manual edit of `items.py` after adoption → `blocked`; duplicate route / symbol / field → `blocked`; ambiguous class across adopted files → `blocked`; concurrent edit after the plan → `WriteError`, nothing written; a failing project test → not applied |

## Recoverable writer (fault injection, `tests/test_evolve_v20_recovery.py`)

A six-file operation on a copy of `inventory-service`: interrupted after 2 files → `rollback` restores the
original tree, `forward` completes it; interrupted before cleanup → `forward` only cleans up; an interrupted
recovery resumes; a file edited after the crash → `needs_attention`, the file untouched, the other restored; a
concurrent change between `prepare` and `publish` → nothing published; unsafe paths and symlinks refused before
staging; a staging I/O error leaves nothing behind. Through the engine: an interrupted `apply` reports
`interrupted` with the pending operation, a new plan refuses until `recover`, and the same evolution applies
cleanly afterwards.

## Cost (`/usr/bin/time -l`, one run each, indicative)

| Step | Time | Peak RSS |
|---|---|---|
| `propose` (hand-written shop) | 0.20 s | 39 MB |
| `adopt` | 0.10 s | 32 MB |
| `plan` shop (copy, audit, invariants) | 0.13 s | 31 MB |
| `apply` shop with its 3 tests | 0.88 s | 77 MB |
| `plan` inventory modify_modules | 0.42 s | 35 MB |
| `apply` inventory modify_modules with 9 tests | 0.95 s | 80 MB |
| `recover` (rollback of an interrupted 6-file operation) | 0.08 s | 30 MB |

# Increment 5: identity, authorization and row-level security

Run of 2026-10-31 on the same machine (macOS, PostgreSQL 16.10 from Homebrew in a throwaway cluster under `/tmp`,
uv 0.11.28 resolving from the local cache, Python 3.13 for the generated projects), through
`tests/test_design_v20_postgresql.py` (`FA_DESIGN_PG=1`, all nine tests) and `tests/test_design_v20_security.py`.
Reports were written with `FA_DESIGN_PG_EVIDENCE`; the numbers below come from them.

## Vertical slice 1: `team-notes` (`../specs/team-notes.json`, OIDC + `principal_claim` + `roles_and_ownership`, in memory)

`tests/test_design_v20_security.py::AuthorizationSliceTests` generates the project and runs its suite through the
trust model (`fixture` profile, `LocalProcessExecutor`, copy of the project, pytest-junit evidence bound to the run):
**54 passed, 0 failed, 0 skipped**; the project is byte-identical afterwards; the audit reports 0 findings and
`authorization_unknown: 0`. Among the executed tests: legitimate access, another member reading per policy,
another member refused on update and delete (403, row unchanged), an admin updating another member's note,
`limited` refused on create/update/delete (403), another tenant refused on the tenant path (403), another tenant's
id presented under the caller's tenant (404, row untouched), nonexistent (404) and malformed (422) ids, `owner_id`
rejected from the client (422), missing or invalid bearer token (401 with `WWW-Authenticate: Bearer`), altered
claims with the original signature, expired and not-yet-valid tokens, wrong audience and issuer, HS256 with the
public key as secret, `alg: none`, unknown and missing `kid`, another key under the same `kid`, malformed tokens,
missing required claims, malformed roles, a token without a usable tenant claim (401), a member of another tenant
(403, not 401), a static JWKS refused outside the test environment, an http JWKS URL refused, an unreachable
provider answered 503. The single-tenant, owner-scoped `journal` fixture (`tests/fixtures/design20/specs/valid/
oidc_owner_scoped.json`, ES256) passes its suite the same way (another member cannot see a colleague's entry:
404; owner-scoped listing; an admin sees and updates it).

## Vertical slice 2: `tenant-projects` (`../specs/tenant-projects.json`, API-key grants + `roles_and_ownership` + `shared_schema_rls`)

`RowLevelSecurityBackendTests`: the cluster gets two extra roles, `fa_app` (`LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
NOCREATEROLE NOINHERIT`) and `fa_bypass` (`LOGIN BYPASSRLS`). The application runs as `fa_app` (`DATABASE_URL`);
migrations and truncation run as the owner (`TENANT_PROJECTS_MIGRATIONS_DATABASE_URL`).

| Test | Result |
|---|---|
| `test_generated_rls_backend_passes_its_suite_through_the_application_role` | generated suite **51 passed, 0 failed, 0 skipped** as `fa_app`; audit: two informational findings (`MIG_RAW_SQL` for the policy statements, `ARCH_IMPORT_LINTER_PRESENT`), nothing above `info`; project unchanged; only `alembic_version` left after the final downgrade; design run 9.4 s wall, 124 MB max RSS of the children (12.0 s / 124 MB in the first run) |
| `test_migration_refuses_a_role_that_bypasses_rls_and_a_missing_role` | `alembic upgrade head` with `TENANT_PROJECTS_APP_ROLE=fa_bypass` fails with "would bypass row-level security", with `fa_missing` fails with "does not exist"; in both cases no table and no `alembic_version` row remain; with `fa_app` it succeeds, `pg_policies` has one policy per tenant table and `relrowsecurity AND relforcerowsecurity` is true for both |
| `test_evolution_protects_the_new_table_and_the_suite_still_passes` | `add_modules: milestones` (`../evolutions/tenant-projects-add-milestones.json`): state `verified`, additive revision with `_protect("milestones")` and `FORCE ROW LEVEL SECURITY`, `tests/test_rls.py` regenerated with the new table, risks name the protection; evolved suite **64 passed, 0 skipped** as `fa_app` |

The twelve demonstrations of `tests/test_rls.py`, all passed through the restricted role: (1) every tenant table has
RLS enabled and forced and one policy whose `USING` and `WITH CHECK` read the context key; (2) the role is not a
superuser, cannot bypass RLS, cannot create roles or databases; (3) without context the owner sees two rows and the
role sees none; (4) without context an insert is rejected by the policy; (5) with the context of tenant A only A's
row is visible, with B's only B's; (6) with the context of A a row of B cannot be inserted, A's rows cannot be moved
to B, and B's row is neither updated nor deleted (0 rows); (7) the context set in a transaction is gone after
commit and after rollback and absent on the next checkout of the same pooled connection (`pool_size=1`); (8) an
insert followed by rollback leaves the owner's count unchanged, the same insert committed adds one; (9) an invalid
context fails the query (`invalid input syntax for type uuid`) and an empty one matches nothing; (10) the role
cannot disable or un-force RLS, drop the policy, truncate the table or grant itself `BYPASSRLS`; (11) 24 concurrent
requests alternating two tenants through the ASGI application (anyio task group, httpx ASGI transport, the real
pool) each see exactly their tenant's rows; (12) an application pointed at a closed port answers 503
`application/problem+json`, not 401 or 404. The module tests add the cross-tenant reference (409 through the
composite foreign key), the foreign id under the caller's tenant (404), ownership across members and admins and
the role needed to delete.

## The earlier examples after the regeneration

The blueprint changes of this increment (API keys as grants, fixed test identities, 404 probes for foreign ids,
malformed-id checks) regenerated the two older snapshots. The same nine-test opt-in run re-verified them on the
throwaway cluster: `orders-platform` 45 generated tests passed (39 in increment 2), the drift negative control
failed as expected, `orders-platform` + `shipments` 58 (50 in increment 3), `orders-platform` + customer columns 46
(40 in increment 4), the backfill and migration-failure controls unchanged. `inventory-service` passes its 10
generated tests through the trust model in the unit suite. The orders contract is still schema 1.1: its declared
`shared_schema_rls` stays application scoping, its manifest records `tenant_strategy_implemented:
shared_schema_application_scoping`, and an evolution plan names it in the risks.

## Cost (one run each, indicative)

| Step | Wall time | Max RSS |
|---|---|---|
| `tests/test_design_v20_security.py` (27 tests, two generated suites through the trust model) | 4.3 s | n/a (in-process) |
| `RowLevelSecurityBackendTests` (3 tests, three `uv sync` + three generated suites on PostgreSQL) | 29.8 s | 124 MB (children, design run) |
| whole opt-in module (9 tests) | 68.7 s | n/a |
| full unit suite, Python 3.13 / 3.9 (532 tests) | 49.4 s / 68.9 s | n/a |

## What this does not show

- The migrations owner on the throwaway cluster is the bootstrap superuser (`fa_owner`): FORCE is verified through
  the catalog flags and through the restricted role, not through a non-superuser owner.
- The OIDC provider is simulated by a key pair generated per test session and a static JWKS accepted only in the
  test environment; no network fetch of a JWKS is exercised (the https requirement, the cache and the refresh
  policy are code-reviewed and unit-asserted, not run against a provider).
- Concurrency covers 24 interleaved requests on one process; it is not a load test.
- Everything executed ran through the local executor (process hygiene, not a sandbox), authorised explicitly by
  the test that generated the project.

# Security acceptance hardening (after increment 5)

Run of 2026-10-31, same machine and tooling as the increment 5 section.

## Auditor precision (`scripts/audit_precision.py --against HEAD`, working tree versus `36211c1`)

| Corpus or project | Before (tp/fp/fn) | After (tp/fp/fn) | Findings before → after |
|---|---|---|---|
| truth16 | 16/0/0 | 16/0/0 | identical per rule |
| truth17 dev | 21/0/0 | 21/0/0 | identical per rule |
| truth17 acceptance | 11/3/2 | 11/3/2 | identical per rule (the three known FPs and two FNs predate this change) |
| truth18 dev | 7/0/0 | 7/0/0 | identical per rule |
| truth18 acceptance | 5/0/0 | 5/0/0 | identical per rule |
| plataforma-citas-medicas (read-only) | n/a | n/a | 52 → 52; `authorization_visible` 127 → 125 (two routes with router-level authentication only lose a kind they never had; four routes gain one through a delegated decision); project edges 9790 → 9791 |
| agentforge-notify (read-only) | n/a | n/a | 15 → 15 |

Holdout not re-scored. The intermediate state of the change (structural evidence without the denial rule and
without following re-exports across source-root spellings) would have moved 54 routes of the clinic project to
"unknown" and added two `SEC_PRIVILEGE_CHANGE_UNVERIFIED` findings on real permission dependencies; both gaps were
closed before the change was accepted, which is what the calibration projects are for.

## Negative controls of the name-based heuristic (`tests/fixtures/regressions_v20/named_authz`)

| Dependency | Shape | Before | After |
|---|---|---|---|
| `require_nothing` | returns the user, no check | `role` | `authorization_named_only`, unknown, `CONTRACT_MUTATION_AUTHZ_REQUIRED` + `SEC_RESOURCE_AUTHZ_UNVERIFIED` |
| `require_admin_none` | `-> None`, no check | `role` | named only, unknown, `CONTRACT_MUTATION_AUTHZ_REQUIRED` (no principal to bind) |
| `require_any_token` | yields a principal for any header | `role` | named only, unknown, both findings |
| `require_authenticated` | 401 only | `role` | named only, unknown, both findings |
| `require_editor` | copies the principal through | `role` | named only, unknown, both findings |
| `require_owner` | never reads the principal | `role` | named only, unknown, `CONTRACT_MUTATION_AUTHZ_REQUIRED` |
| `get_ctx` over `require_admin_none` | nested name-only | nothing visible | named only (chain named), unknown, both findings |
| `current_superuser` (external) | not in the project | `role` | named only ("implementation not in the project"), unknown, both findings |
| `require_admin`, `require_scope("x")`, `RoleChecker("admin")`, `get_admin_ctx`, `guard`, `gate` | real decisions (condition, closure, `__call__`, nested, 403, delegated) | `role` | `role` with the line of the decision |
| router `dependencies=[Depends(require_admin)]` / `[Depends(require_nothing)]` | | `role`+`router_dependencies` / `role`+`router_dependencies` | `role`+`router_dependencies` / nothing (named only) |

## JWKS client over HTTPS (`team-notes`, generated `tests/test_jwks.py`, through the trust model)

`team-notes` now passes **71 generated tests** (54 in increment 5): the 17 new ones run against an HTTPS identity
provider started on 127.0.0.1 with a session certificate authority trusted only through `oidc_ca_bundle`
(certificate and hostname verification on; strict verifiers required subject and authority key identifiers, key
usage and server-auth usage on the certificates, which the generated tests now emit). Scenarios and outcomes:
valid fetch and signature (one hit), cache (one hit for five requests), rotation (a new `kid` is 401 until the
30 s interval passes, then fetched once), ten forged `kid`s cause at most one fetch, a key replaced under the same
`kid` is 401 until the cache expires, a removed key is 401 after expiry, malformed JSON / invalid set / symmetric-only
set / JSON list / HTTP 500 / HTTP 404 / oversized body are all 503 without `WWW-Authenticate`, a 2 s provider with a
0.5 s timeout is 503 in under 1.9 s, the system trust store and a foreign CA both fail the handshake (503, zero
provider hits), an outage with no cache is 503 and the provider is not hammered (one hit for two requests) and
recovers after the interval, a warm cache serves through an outage for the stale window only, eight concurrent
cold starts fetch once. The journal fixture (ES256) runs the same module.

## Row-level security boundary (`tenant-projects`, PostgreSQL 16, role `fa_app`)

Generated suite **53 passed, 0 failed, 0 skipped** as the restricted role (51 in increment 5): the two new
demonstrations show (13) that a `set_config` statement executed as the application role switches the tenant of
its transaction (the same text as a bound parameter switches nothing and the context stays on tenant A) and (14)
that the role cannot read `pg_authid` or `alembic_version`, create a table or a policy, or take ownership. The
negative controls (BYPASSRLS and missing roles refused by the migration) and the evolution with a protected table
(66 tests) pass unchanged. Opt-in class wall time 33.8 s; design run 12.8 s, 124 MB max RSS of the children.

## What this does not show

- The precision figures are against the dev and acceptance corpora and two calibration projects; the holdout was
  not re-scored, so the absence of regressions there is not claimed.
- The HTTPS provider is local and cooperative: no real identity provider, no proxy, no certificate revocation, no
  HTTP/2, no redirect handling was exercised.
- The RLS boundary tests demonstrate what the mechanism cannot stop; they do not claim an injection is impossible
  in the generated code (every generated statement is parameterised, and that is a property of the code, not a
  guarantee against future code).

# Acceptance of v2.0.0 (`tests/test_acceptance_v20.py`)

Run of 2026-10-31, same environment. One scenario per persistence kind, each on the plugin's own example contract,
each chaining requirements → reasoned architecture → generation → audit → tests through the trust model → evolution
with a new **owned** module → verification → compatibility.

| Step | `team-notes` (in memory, OIDC + claim binding + roles and ownership) | `tenant-projects` (PostgreSQL 16 as `fa_app`, grants + roles and ownership + RLS) |
|---|---|---|
| Architecture | `layered`, deterministic, with rejected alternatives | `modular_monolith` |
| Generated suite | 71 passed, 0 skipped; audit 0 findings | 53 passed, 0 skipped; two informational findings |
| Evolution | `boards` (owned): planned, verified, applied; `core/security.py`, `core/authz.py` untouched | `milestones` (owned, with update): planned, verified, applied; additive revision with `_protect("milestones")` and `owner_id`; two revisions, one head, historical file byte-identical |
| Evolved suite | 93 passed, 0 skipped; every test that passed before still passes | 72 passed, 0 skipped; same property |
| Routes | 10 → 15 | 11 → 15 |
| Manifest | history generate → evolve; every file `managed_unchanged`; `security` record equal to the guarantees | same; `row_level_security: true` |
| Repeat of the same contract | `noop` | not repeated (database re-used for the whole cycle; it ends with `alembic_version` only) |
| Wall time | 21.5 s | 41.7 s |

Defects the scenario found before passing (both fixed in the blueprint, snapshots regenerated): the policy module
named the owned entities in its docstring and an evolution adding an owned module was blocked for needing to
regenerate a non-integration file; the generated PostgreSQL conftest refused a database holding only the empty
`alembic_version` table left by its own downgrade, so a second suite on the same disposable database failed before
testing.

# v2.1 increment 2: phased migrations on the fifth example (`parcels`, PostgreSQL 16)

`FA_DESIGN_PG=1 python3 -m unittest discover -s tests -p test_migrations_v21_postgresql.py` on 2026-10-31: one
throwaway PostgreSQL 16.10 cluster (`tests/_pg_support.py`), eight databases, four tests, **42.2 s wall clock,
127 MB peak RSS** (`/usr/bin/time -l`), CPython 3.13.5 on the host, the generated project synced by uv from the local
cache (SQLAlchemy 2.0.55, asyncpg 0.30.0, Alembic 1.20.1). Every number below was measured in that run; the
evolution reports, the migration evidence and the timings were saved with `FA_DESIGN_PG_EVIDENCE`. The unit module
`tests/test_migrations_v21.py` (18 tests, no database) covers the contract validation, the planning, the generated
files, the refusal of SQL in contracts, the in-memory refusals, the manifest state machine, the contract
preconditions against evidence documents and the example files.

## The vertical slice (`test_01`)

| Step | What happened | Measured |
|---|---|---|
| Baseline | `alembic upgrade head` on the committed `parcels` snapshot (one revision), 2 500 rows inserted with `reference` only | 2.45 s (uv startup included) |
| Expand evolution (`parcels-expand-carrier.json`) | plan: create 6 (`ops/__init__.py`, `ops/backfill.py`, `ops/backfills.py`, `tests/test_backfill.py`, two revisions), modify 8, unchanged 25, kept 1 (the historical revision); routes 0 added / 0 changed / 0 removed; phases `expand` `8546a10c95ec` (transactional) and `index` `c863863c3d2c` (non-transactional); backfills `66636e4277641692` (`carrier`, `constant`) and `5821fb0e87d4b355` (`tracking_token`, `uuid`), application version `ba13f9bbb39e…`; verified in a copy against a separate empty database through the trust model: 18 generated tests selected, 18 passed (7 of them `tests/test_backfill.py`); written | `verified`, `applied`; 7.57 s plan + verification + write |
| Expand and index revisions on the production database | `alembic upgrade head`: two nullable columns, the progress table, `ix_shipments_carrier` built `CONCURRENTLY` | 0.38 s |
| Clean stop | `run 66636e4277641692 --batch-size 200 --max-batches 3` | 600 rows, 3 batches, state `running` |
| Two workers | two `run --batch-size 50 --max-batches 4` started together | exit codes `[0, 3]`; exactly one progressed: 800 rows filled, checkpoint 800 |
| SIGKILL | the runner (`.venv/bin/python -m parcels.ops.backfill run --batch-size 50 --batch-pause-ms 200`) killed after the checkpoint passed 1 000 | checkpoint == rows filled (they commit together), fewer than 2 500 |
| Resume | `run --batch-size 200` | `complete`, `rows_processed` 2 500 exactly: every row once across the stop, the two workers, the kill and the resumption; `count(DISTINCT carrier)` 1; a second run processes 0 rows | 0.32 s |
| Concurrent writes (`tracking_token`) | the application wrote 3 tokens before the run; an "old application" thread inserted 40 rows with NULLs while `run --batch-size 150 --batch-pause-ms 10` ran | the run processed 2 508 rows in 18 batches and 2 passes (0.267 s) and reported `complete`; the 3 application values were untouched; 29 rows inserted after the last count stayed NULL; the next run re-counted, reopened the operation and filled exactly those 29; final record 2 537 rows, 19 batches, 3 passes; 1.76 s for the concurrent phase |
| The other column | the 40 late rows were NULL in `carrier` too: `run 66636e4277641692 --max-seconds 0.001` on the `complete` operation re-counted, reopened it and stopped before its first batch (`running`, 0 rows); the next run filled 40 | final record 2 540 rows, 20 batches, 2 passes |
| Checkpoints that cannot be trusted | `last_key = 'garbage'` → exit 4 (`corrupt`); a foreign checksum → exit 4 (`another definition`); an unknown operation → exit 7 | nothing touched in the three cases |
| Evidence | `verify --application-version ba13f9bbb39e… --output evidence.json` | `ok`; `alembic_version` at the expected head; both operations complete with 0 pending rows; `ix_shipments_carrier` valid |
| Contract refused | `plan` of `parcels-contract-carrier.json` without `--authorize-contract` → `blocked` (`contract_authorized`); with the flag and the committed sample evidence → `blocked` (`contract_evidence_fresh`: the sample is dated 2026-10-01 by design) | nothing written |
| Contract evolution | `apply --authorize-contract --evidence evidence.json`: the 12 preconditions true for each of the two operations; phase `contract` `a82f4c68b677` (non-transactional, reversible); verified in a copy on a fresh database: 18 generated tests selected, 14 passed, 4 skipped (the insert-based backfill tests skip once every change is contracted); written | `verified`, `applied`; 7.60 s |
| The revision refuses a NULL that reappeared | `UPDATE shipments SET carrier = NULL` for one row, then `alembic upgrade head` | fails with `1 row(s) still NULL`; the column stays nullable |
| Contract revision applied | the row fixed, `alembic upgrade head` | 0.35 s; `carrier` and `tracking_token` `NOT NULL`; no `ck_shipments_%_not_null` constraint left; one head |
| Reversible | `alembic downgrade -1`, then `upgrade head` | `carrier` nullable again with every row kept (2 540), then `NOT NULL` again |
| Final evidence and discovery | `verify`; `discover` | `ok`, no nullable operation; 4 revisions, no historical file modified |
| Fresh environment | a new database: `alembic upgrade head` (all four revisions), `verify`, `run 66636e4277641692` | 0.35 s; both operations recorded `empty-table-at-contract` by the contract revision; evidence `ok`; the run reports `complete` with 0 rows |

## Concurrent indexes (`test_02`)

An index named `ix_shipments_carrier` with another definition (`(carrier, reference)`) created by hand: the index
revision fails with `different definition` and leaves it alone. The same name with the expected definition but
`indisvalid = false` (an interrupted build): the revision drops and rebuilds it (0.34 s); it is valid afterwards. A
second `upgrade head` is a no-op; `downgrade -1` drops the index concurrently; `upgrade head` recreates it; one head.

## Connection failure and time limit (`test_03`)

A URL to a database that does not exist: exit 2, `database connection failed`, nothing written. 300 rows and
`--max-seconds 0`: exit 0 with `running` and 0 batches, never success; the next run with `--batch-size 50`
completes the 300.

## Row-level security (`test_04`, `tenant-projects`)

A `region` column with a constant backfill on the RLS example (`FORCE ROW LEVEL SECURITY`): the plan names the risk; as
the restricted application role the runner exits 5 (`row-level security`) and the 20 rows stay NULL; as the owner it
completes the 20.

## Defects the scenarios found before passing (all in the generated runner or its tests, fixed in the template)

1. The advisory lock and the RLS guard ran before the first explicit transaction and SQLAlchemy had already started one by autobegin: every generated backfill test failed on a real database (`InvalidRequestError`). The runner commits the autobegun transaction first; the session-level lock survives it.
2. asyncpg raises its own exception classes while connecting and SQLAlchemy wraps them only once connected: a connection failure escaped with exit code 1 instead of 2. A `_connect` helper classifies anything raised while connecting as exit code 2; dropped connections map to 2 and other database errors to 7.
3. The generated `test_verify_reports_the_state_of_this_database` ran only the first pending operation before asking for the evidence; with two phased columns the evidence was honestly not ok. The generated tests now run every pending operation.
4. A `complete` operation ignored NULL rows an old application version inserted afterwards (a second run answered "already complete"). A run on a complete operation now re-counts and reopens it for exactly those rows; a run that finds nothing still processes nothing.
5. After the contract phase the generated insert-based backfill tests still targeted the first (now `NOT NULL`) column. They skip once every change is contracted.
6. The record the contract revision writes for an empty table (`empty-table-at-contract`) was refused by the runner as a foreign checkpoint and flagged by the evidence. Both recognise the sentinel; the fresh-environment step of the slice proves it.

Three defects were in the test harness itself: psql's command tag parsed as a row, a SIGKILL sent to the `uv run`
wrapper (the runner survived as an orphan and finished), and an INSERT naming columns the RLS example does not have.

## What this does not show

Tables larger than 2 540 rows (the batch loop is O(batch) per step by construction; the final count is a scan of
the NULL rows of the column); primary keys other than the generated `uuid` `id`; backfills as the application role
under RLS; a real deployment with two application versions (the thread above is a stand-in); Podman or a container
sidecar for the database (deferred); renames, drops and type changes (not in v2.1).

# v2.2 increment 1: renames in transition on the fifth example (`parcels`, PostgreSQL 16)

`FA_DESIGN_PG=1 python3 -m unittest discover -s tests -p test_renames_v22_postgresql.py` on 2026-10-31: one throwaway
PostgreSQL 16.10 cluster, four tests, **43.0 s wall clock, 124 MB peak RSS**; the committed `parcels` snapshot is
version **A** (it knows `reference` and `weight_kg` only) and the project evolved with
`../evolutions/parcels-rename-label.json` is version **B** (both names). The two run as separate processes from their
own virtual environments (`.venv/bin/python`, each wrapping its application with `TestClient` and answering requests
read from its standard input) against the same expanded database. Every number below was measured in that run.

## Coexistence of A and B (`test_01`)

| Step | Result |
|---|---|
| A's baseline applied, 1 500 live rows (one third with `weight_kg` NULL) | 2.5 s (uv startup included) |
| B's rename evolution verified in a copy (its own suite on a separate database through the trust model: 21 tests, 21 passed, among them the former-name create and update tests and the sync-trigger test; audit 0 → 0 findings) and applied | `verified`, `applied`; plan: create 5, modify 8, unchanged 25, kept 1; phases `expand` + `transition`; 11.0 s |
| Expand revision on the shared database | `label`, `weight` added nullable, two sync triggers enabled, 1 500 rows still NULL in `label` (the backfill's job); 2.5 s |
| A creates a row with `reference` and `weight_kg` | 201; the database shows `label` and `weight` already equal (mirrored by the trigger) |
| B reads A's row and a pre-backfill row | `label == reference`, `weight == weight_kg`; a row the backfill has not touched reads correctly through the old column |
| B creates with `label` and with the former name `reference` | 201 both; both columns equal in the database |
| A updates B's row, B reads it; B updates A's row through either name, A reads it | the old column is the truth and the mirror follows: every read sees the last write |
| A sets `weight_kg` to NULL | both columns NULL |
| 30 interleaved updates of one row from both processes (15 with the old name, 15 with the new) | all 200; final pair equal (`b-14 | b-14`), the value one of the sent ones; 0.05 s |
| A rolled-back update | both columns untouched |
| Backfill of `label` killed by SIGKILL after 300 processed rows, resumed while A inserted 20 rows | the mirrored rows were never fewer than the processed ones; resumption `complete`: 1 500 rows, 12 batches, 1 pass, 0.52 s; A's 20 late inserts all 201 and mirrored by the trigger; 0 rows where `label` differs from `reference` |
| Backfill of `weight` | `complete`: 1 000 rows in 2 batches (the 500 NULL weights already agreed) |
| Evidence (`verify`, schema 1.1) | `ok`; both operations `columns_agree` with 0 mismatches, triggers present and enabled, source of truth `from_column` |
| After the backfill | A still creates; B lists rows with `label == reference` on every one |

## Backfill controls (`test_02`)

400 rows; the first row held under `FOR UPDATE` by another session: the run ends `incomplete` with 399 rows and 1
pending; released, the next run completes with 1. Drift injected behind a disabled trigger (`reference` changed): `verify`
reports 1 mismatch and `columns_agree: false`; a run reopens the operation, fixes 1 and the mirror takes the old
column's value. A disabled trigger is a problem the evidence names even when the columns agree. Corrupt and foreign
checkpoints keep their exit code 4. The expand revision downgrades without data loss (triggers and new columns gone,
400 rows intact) and upgrades again.

## Adversarial (`test_03`) and row-level security (`test_04`)

A hand-made `label integer` column on the database: the expand revision fails with "already exists", the version
table and the schema are unchanged (the revision is transactional), no trigger is left behind. A stale baseline digest,
a hand edit on a regenerated integration point, an altered historical revision, a second Alembic head and a premature
contract of the rename are blocked by the plan with their precondition. On `tenant-projects` (policies with
`FORCE ROW LEVEL SECURITY`), a rename of `name` to `title` applied as the owner: the application role's insert under its
tenant context is mirrored by the trigger inside the policy; the backfill refuses the application role (exit 5) and
completes as the owner; a rename of the tenant column is refused.

## Defects the scenarios found before passing

- The evidence read `pg_trigger.tgenabled` as the single-byte `"char"` type, which asyncpg returns as bytes: every
  enabled trigger was reported disabled. The query casts to text.
- The generated trigger test inserted only the renamed column of each rename; with a NOT NULL column renamed next to
  it the insert failed. Each rename now carries a complete insert statement for its table.
- Generated lines over the project's 120-column limit (trigger DDL, model comments, runner evidence, the renames
  literal of the tests) were reformatted; the evolved project passes its own Ruff configuration.

## What this does not show

A real distributed deployment (load balancers, rolling restarts, clock skew): the two versions are two processes on one
machine sharing one database. Tables beyond 1 500 rows. Retire and contract of a rename are the next section.

# v2.2 increment 2: retire and the destructive contract on a real PostgreSQL

`FA_DESIGN_PG=1 python3 -m unittest discover -s tests -p test_retire_contract_v22_postgresql.py` on a throwaway
PostgreSQL 16.10 cluster, macOS, CPython 3.13.5, uv: **3 tests OK in 94.5 s, 134 MB** (the measured
timings are in `retire-timings.json` of the evidence directory when `FA_DESIGN_PG_EVIDENCE` is set). Three
generations as separate processes from their own virtual environments on one database: **A** (in `test_01` the parcels
project exactly as v2.1.0 published it, `tests/fixtures/compat/generation-1.3/parcels`, which announces no
`application_name`; in `test_02` the regenerated snapshot, which announces the baseline tag), **B** (the rename
evolution) and **C** (the retire evolution). Every number below was measured in that run.

## B and C on the retire schema (`test_01`)

| Step | Result |
|---|---|
| A's baseline, 1 500 live rows; B's rename evolution applied, expand revision, both backfills complete | as in increment 1 |
| B's evidence (`verify --application-version <B digest>`), then C's retire evolution verified in a copy (its own suite on a separate database through the trust model: 20 selected, 16 passed, 4 skipped, 0 failed, among them the sync-trigger mirror and contradiction tests and the schema-drift test that tolerates the retired columns; audit: no new blocking finding) and applied | 12.1 s |
| Retire revision on the shared database | `label` NOT NULL, `weight` still nullable, `reference` and `weight_kg` kept, both function comments `truth=label` / `truth=weight` with `retired_at`; 2.6 s |
| C creates with `label` and `weight` only | 201, no `reference` in the response; the database shows `reference` and `weight_kg` filled by the trigger; B reads it under both names, A under the old one |
| B creates with the former name; C reads it | 201; C sees `label` only |
| A creates with `reference` on the retire schema | 201; mirrored; C reads it (tolerated: nothing is lost, A is outside the retire contract) |
| Partial updates: C changes `weight`, B sets `weight_kg` to NULL, B renames through `reference` | the pair stays equal; NULL propagates; C reads B's value |
| B receives both names in one request (different values; equal values) | 422 both times: one name per request; one name alone is 201 |
| One statement sets `reference` and `label` to different values (UPDATE; INSERT) | refused by the trigger (SQLSTATE 23514, "set to different values in one statement"); the row is untouched; nothing inserted |
| NULL through the old name after the retire | refused: the new column carries the NOT NULL |
| 30 interleaved updates of one row from B (`reference`) and C (`label`) | all 200; the pair equal; the value one of the sent ones; 0.06 s; 0 rows differ in either pair afterwards |
| Evidence written by C's runner (schema 1.2) | `ok`; both operations `state_declared: retire`, `sync.truth` the new column, `retired_at` set, `columns_agree`, no unmanaged dependent; the sessions list B's tag, C's tag and the empty name of A |
| Retire revision downgraded, then upgraded again | `label` nullable again and `truth=reference`, 0 rows differ; then NOT NULL again |

## The destructive contract (`test_02`)

600 rows; A, B and C connected. The plan is refused while A and B are connected (`contract_deployments_identified`:
A's baseline tag and B's tag are not `compatible_tags`), with or without the flags; B stopped, A still blocks; A
stopped, fresh evidence lists C's tag only and the plan is `planned` with `--authorize-contract` and
`--confirm-irreversible`, blocked without the phrase, and the command line prints the phrase it needs. The
destructive evolution is verified in a copy (D's own suite on a fresh database runs the chain through the drop: 20 selected,
14 passed, 6 skipped, 0 failed; audit: no new blocking finding) and applied: 18.6 s (the suite of the unit tests was running at the same time). Then, on the live database, each of these makes `alembic upgrade head` fail with the schema intact
(`alembic_version` unchanged, both old columns present, both triggers enabled): a manual view on `reference`
("dependents the tool does not manage"), a manual index on `reference`, an unknown trigger whose function names
`reference`, the sync trigger disabled, a row where the columns differ (fixed by a backfill run), another session
holding every row under `FOR UPDATE` (`lock timeout`, 5.7 s: the revision fails rather than queue). C inserts five
rows after the evidence; the drop then succeeds in 0.56 s: old columns, triggers and functions gone, the late rows
present. C keeps working (create, list); B restarted answers 500 (it still maps the dropped column: incompatible,
as documented). The evidence of the contracted state reports `source_present: false` and no trigger. The downgrade
(0.54 s) recreates `reference` and `weight_kg` from the new columns' current values (0 rows differ), NOT NULL on
`reference`, both triggers enabled with `truth=label`; B restored reads a row C created after the drop under both
names; the upgrade drops again; a second contract of the same operation is `contract_operation_pending`.

## Wrong order (`test_03`)

On a live B project: a contract before the retire is `contract_rename_retire_first`; a retire without evidence is
`contract_evidence_valid`; a retire with evidence from a database whose backfill never ran is `retire_columns_agree`
and `retire_backfill_complete`; the live database was never touched.

## Defects the scenarios found before passing

- The generated runner and the drop revision used PostgreSQL's word boundaries (`\m`, `\M`) in a Python regular
  expression as well: `verify` crashed on every rename. Python's `\b` for trigger sources, PostgreSQL's for `~*`.
- The generated contradiction test set the old column to the value it already had, so only one column changed and the
  trigger mirrored instead of refusing; it now uses three distinct literals per type (decimal had two equal ones).
- The first boundary rule ("both names with different values") let equal values through, where `extra="forbid"` had
  already refused the second key: the rule is now one name per request, equal or not, with the reason in the 422.
- `order` is a reserved word: a database of that name cannot be created.

## What this does not show

A distributed deployment: three processes on one machine sharing one database; the sessions check is a snapshot of
`pg_stat_activity` and cannot see instances that connect intermittently. A connection failure in the middle of the
drop transaction (PostgreSQL rolls it back; not injected here). Tables beyond 1 500 rows; a full-table downgrade copy
on a large table is offline by design and was not timed at scale.

# v2.2 increment 2, acceptance hardening of the destructive contract

`FA_DESIGN_PG=1 python3 -m unittest discover -s tests -p test_destructive_acceptance_v22_postgresql.py` on a throwaway
PostgreSQL 16.10 cluster, macOS, CPython 3.13.5: **5 tests OK in 112.9 s, 131 MB** after the fixes. The same
module against the engine before the fixes (HEAD `9ee9714`) is the "before" column. Nothing here adds a capability.

## Defects, before and after

| Scenario | Before the fixes (`9ee9714`) | After |
|---|---|---|
| A field with a default (`state default 'open'`) renamed to `status`; `INSERT (code, status) VALUES ('T-1', 'closed')` | planned and applied; the row read `state = 'open', status = 'open'`: the explicit value was lost to the default | the plan refuses `rename_defaulted_column_unsupported`; the trigger keeps no precedence branch |
| B (a generation that maps the old column) reconnects after the plan, before `alembic upgrade head` | the revision dropped the column with B connected (exit 0) | the revision refuses before locking: "sessions that may still map the old column: ['parcels@… x1 as fa_owner']"; schema intact |
| The contract allows 0.001 h of evidence age; the revision runs 5 s after the plan | the revision dropped the column (exit 0) | the revision refuses: "the evidence this revision was planned from is 0.0 h old (limit 0.001 h): re-plan"; schema intact |
| A downgrade of the drop while another session holds every row `FOR UPDATE` | no `lock_timeout` in the downgrade: it queues until the holder commits (by inspection of the generated revision) | the downgrade fails after the lock timeout; the retry succeeds once the lock is released |

## Transition integrity (`test_01`, raw SQL on the retire schema, 40 rows)

| Write | Result |
|---|---|
| INSERT / UPDATE through the old name only | mirrored into the new column, same statement |
| INSERT / UPDATE through the new name only | mirrored into the old column |
| Both names, equal values (INSERT and UPDATE) | accepted |
| Both names, different values (INSERT; UPDATE of the NOT NULL pair; UPDATE of the nullable pair) | refused, SQLSTATE 23514, the row untouched, nothing inserted |
| UPDATE of another column (`weight`, `created_at`) | the pair left alone |
| Explicit NULL on the nullable pair, through either name | both NULL |
| Explicit NULL on the NOT NULL pair, through either name; INSERT without either name | refused, SQLSTATE 23502 |
| ROLLBACK after writes through both names | both columns untouched |
| Two sessions on one row (`reference` in a held transaction, `label` waiting) | the second waits for the row lock; the last committed write wins on both columns |
| A (v2.1.0, old name only) after the retire: create, set `weight_kg` to NULL | mirrored; C reads it; nothing lost |

## Dependency matrix (`test_03`, 200 rows, D planned, each object created before `alembic upgrade head`)

| Dependency on `shipments.reference` | Evidence lists | Plan | Revision | Schema |
|---|---|---|---|---|
| index | `index ix_dep_reference` | blocked | refused ("dependents the tool does not manage") | intact |
| partial index (`WHERE reference <> ''`) | `index ix_dep_partial` | blocked | refused | intact |
| unique constraint | `constraint uq_dep_reference on table shipments` (and its index) | blocked | refused | intact |
| check constraint | `constraint ck_dep_reference on table shipments` | blocked | refused | intact |
| foreign key from another table | `constraint dep_audit_ref_fkey on table dep_audit` and the unique target | blocked | refused | intact |
| view | `rule _RETURN on view dep_view` | blocked | refused | intact |
| materialized view | `rule _RETURN on materialized view dep_matview` | blocked | refused | intact |
| row-level-security policy | `policy dep_policy on table shipments` | blocked | refused | intact |
| generated column (`upper(reference)`) | `default value for column dep_upper of table shipments` | blocked | refused | intact |
| trigger of other code with `UPDATE OF reference` | `trigger dep_trigger on table shipments` | blocked | refused | intact |
| trigger of other code whose body names the column | `dep_audit` (`other_triggers`, `mentions_column`) | blocked | refused ("named by code the tool does not manage") | intact |
| function naming table and column | `public.dep_report` (`functions_mentioning`) | blocked | refused ("named by code") | intact |
| extended statistics | `statistics object dep_stats` | blocked | refused | intact |

Every catalog kind comes from `pg_depend` on the column (the column's own default and NOT NULL are the only dependents
considered the tool's); the two textual kinds come from the trigger sources of the table and from `pg_proc`. With
every dependency removed the same revision applies. `RESTRICT` is the last line, not the first: PostgreSQL drops
indexes, constraints and statistics that involve the column together with it, which is why they are refused by the
checks rather than left to the statement.

## Atomicity and failures (`test_04`, `test_05`, 300 rows)

| Case | Result |
|---|---|
| Another session holds every row `FOR UPDATE`; `alembic upgrade head` | fails after the 5 s lock timeout; `alembic_version`, both old columns and both triggers unchanged |
| The revision's backend terminated (`pg_terminate_backend`) while it waits for the lock | the client reports the closed connection; nothing of the revision reached the database |
| B reconnects between the plan and the upgrade | refused before the lock (sessions re-checked by the revision); schema intact |
| Rows written by C after the evidence; retry of the upgrade with nothing in the way | the drop succeeds; C keeps working |
| A trigger of the same name as the sync trigger exists when the downgrade runs | the downgrade fails at `CREATE TRIGGER`; the recreated column is gone again; `alembic_version` still at the drop revision (one transaction) |
| Another session holds every row while the downgrade runs | fails after the lock timeout; nothing recreated |
| Retry of the downgrade | both columns back, 0 rows differ, both triggers enabled; upgrade again |
| The contract tolerates 0.001 h of evidence age; the upgrade runs 5 s after the plan | refused by the revision; schema intact |

## What this does not show

A network partition in the middle of the `DROP COLUMN` statement itself (PostgreSQL rolls the transaction back on its
side; the client sees an error; not injected here). A distributed deployment. The additive revision of a defaulted
column with a long name may exceed the generated project's 120-column Ruff limit (v2.1 template, cosmetic, untouched).
