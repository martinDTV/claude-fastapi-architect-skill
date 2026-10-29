# Verification record

Label granted: **`verified`** for the stack below, following `references/reference-implementation-strategy.md`.

| Item | Value |
|---|---|
| Date | 2026-10-29 |
| Machine | macOS (Darwin 27.0, Apple Silicon), local only, no network services |
| Python | 3.13.5 (venv created by uv 0.11.28 from `uv.lock`) |
| FastAPI / Starlette | 0.162.0 |
| Pydantic / pydantic-settings | 2.14.1 / 2.x (locked) |
| SQLAlchemy | 2.0.55 (asyncio extra) |
| asyncpg | 0.30.0 |
| Alembic | 1.20.1 |
| httpx (tests) | 0.28.1 |
| PostgreSQL | 16.10 (Homebrew), throwaway cluster created by the test suite with `initdb`/`pg_ctl` in a temporary directory, deleted afterwards |

## Commands and results

```text
$ uv sync --extra dev
Resolved 33 packages

$ uv run pytest -q
......................
22 passed in 3.95s

$ uv run ruff check src tests alembic
All checks passed!
$ uv run ruff format --check src tests alembic
35 files already formatted

$ python3 ../../../../scripts/audit_fastapi.py .
files scanned: 36  findings: 3 (high 0, medium 2, low 0, info 1)
  alembic/versions/0002_rls.py:32  MIG_RAW_SQL                 info   (dynamic SQL: role name from configuration)
  src/ledger/modules/ledger/antipatterns.py:20  TX_READ_MODIFY_WRITE      medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:29  SA_ASYNC_LAZY_LOAD_RISK   medium (deliberate, reproduced in tests)
$ python3 ../../../../scripts/audit_fastapi.py . --exclude-path "*/antipatterns.py" --ignore MIG_RAW_SQL --fail-on info
exit 0 (no findings)
```

## What each test proves (22 tests, all against PostgreSQL)

| Requirement | Test | Evidence |
|---|---|---|
| Authorized operation | `test_authz.py::test_member_can_create_and_read_an_account` | member creates and reads an account, `ETag: "1"` |
| Denied operation | `test_authz.py::test_only_owners_can_delete_accounts` | member delete → 403 Problem Details; owner → 204 |
| Unauthenticated / unknown identity | `test_authz.py::test_missing_or_unknown_demo_identity_is_rejected` | 401 with the demo-auth notice |
| Cross-tenant access denied | `test_authz.py::test_cross_tenant_access_is_denied_for_every_verb`, `..._invisible_even_to_a_member` | 404 on GET/POST/PATCH/DELETE for a non-member; a foreign account id is invisible inside another tenant |
| RLS below the application | `test_rls.py` | app role with context A sees A only; without context sees zero rows; cross-tenant INSERT rejected by the policy; owner role bypasses policies (hence never used by the app) |
| Concurrency without lost updates | `test_concurrency.py::test_fifty_concurrent_deposits_lose_nothing` | 50 concurrent deposits → balance 500, version 51, 50 entries |
| Overdraft under concurrency | `test_concurrency.py::test_overdraft_is_rejected_atomically_under_concurrency` | 5 concurrent withdrawals of 30 from 100 → exactly three 201 and two 409 |
| Optimistic concurrency | `test_concurrency.py::test_if_match_protects_renames`, `..._exactly_one_wins` | 428 without If-Match, 412 when stale, exactly one of two concurrent renames wins |
| Rollback | `test_rollback.py` | injected failure after the balance update leaves balance, version and entries unchanged; overdraft leaves no entry |
| Idempotency | `test_idempotency.py` | replay returns the first result; 10 concurrent duplicates with one key create one entry; keys scoped per tenant |
| Migration from empty | `test_migrations.py::test_upgrade_from_empty_then_check` | tables, 3 policies, RLS enabled and forced, `alembic check` clean |
| Migration from the previous revision | `test_migrations.py::test_upgrade_from_the_previous_revision` | 0001 with data → head → downgrade 0001 |
| Anti-pattern reproduced and fixed | `test_antipatterns.py` | read-modify-write loses a deposit (110 instead of 120), atomic UPDATE keeps both; implicit lazy load raises `MissingGreenlet`, `selectinload` fixes it |

## Defects found while verifying (and fixed before labelling)

- RLS policy cast: after a rolled-back transaction PostgreSQL keeps the custom setting defined with an empty string, so `current_setting('app.tenant_id', true)::uuid` failed with `invalid input syntax for type uuid: ""`. Policies now use `NULLIF(current_setting(...), '')::uuid`.
- Reading an ORM instance after `rollback()` or `commit()` without the tenant context: the row was invisible (RLS) or the refresh raised `MissingGreenlet`. The optimistic rename now returns the row from `UPDATE ... RETURNING` inside the transaction, and the 412 path reads the current version before rolling back.

## Not verified

- Behaviour under PgBouncer or another pooler, other PostgreSQL major versions, or other FastAPI/SQLAlchemy versions.
- Load beyond 50 concurrent in-process requests.
- Running `compose.yaml` on a machine without Docker.

Re-run this record after upgrading any dependency; the label drops to `draft` until it is renewed.
