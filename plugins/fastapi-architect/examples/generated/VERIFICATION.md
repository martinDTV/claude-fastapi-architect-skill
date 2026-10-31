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
