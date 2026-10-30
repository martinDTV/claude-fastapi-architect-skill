# Verification record

Label granted: **`verified`** for the stack below, following `references/reference-implementation-strategy.md`.

| Item | Value |
|---|---|
| Date | 2026-10-29 (initial record), renewed 2026-10-29 with the v1.6 adversarial tests and the v1.7 tenant-binding tests |
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
.............................
29 passed in 7.36s            (22 in the initial record; 5 adversarial tests added for v1.6; 2 tenant-binding tests for v1.7)

$ uv run ruff check src tests alembic
All checks passed!
$ uv run ruff format --check src tests alembic
41 files already formatted

$ python3 ../../../../scripts/audit_fastapi.py .          (auditor 1.7.0, schema 1.5)
files scanned: 38  findings: 6 (high 2, medium 3, low 0, info 1)
  alembic/versions/0002_rls.py:32  MIG_RAW_SQL                      info   (dynamic SQL: role name from configuration)
  src/ledger/modules/ledger/antipatterns.py:20   TX_READ_MODIFY_WRITE            medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:29   SA_ASYNC_LAZY_LOAD_RISK         medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:71   SA_SESSION_SHARED_CONCURRENTLY  high   (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:102  BG_REQUEST_SCOPED_RESOURCE      high   (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:142  SEC_RLS_CONTEXT_FROM_REQUEST    medium (deliberate, reproduced in tests)
  contracts: 13 routes, authenticated 12, public 1, unauthenticated and not public 0, authorization visible 9, authorization unknown 1 (the deliberate unbound route)
$ python3 ../../../../scripts/audit_fastapi.py . --exclude-path "*/antipatterns.py" --ignore MIG_RAW_SQL --fail-on info
exit 0 (no findings)
```

## What each test proves (29 tests, all against PostgreSQL)

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
| Shared AsyncSession (v1.6) | `test_adversarial.py::test_one_session_shared_by_two_tasks_mixes_their_transactions` | two units of work on one session share one transaction: the unit that rolls back discards the other's +5 (balance 0); one session per task keeps the +5 (balance 5). asyncpg serialises the statements with a lock, so nothing raises: the damage is silent |
| Request-scoped session in a task (v1.6) | `test_adversarial.py::test_request_session_handed_to_a_task_loses_its_context_after_the_response` | after the request scope closes, the task's write runs without tenant context and row-level security refuses it; a task that opens its own session and sets the context from ids succeeds |
| Cancellation by timeout (v1.6) | `test_adversarial.py::test_cancellation_by_timeout_leaves_nothing_committed_and_the_connection_usable` | `wait_for` cancels during the wait after the UPDATE; rollback leaves balance 0; the session and pool keep working afterwards (balance 7) |
| Lost response, retried (v1.6) | `test_adversarial.py::test_lost_response_retried_without_a_key_duplicates_and_with_a_key_does_not` | without a key the retry applies the deposit twice (20); with `Idempotency-Key` the retry replays the stored result (30, three entries in total) |
| RLS configuration invariants (v1.6) | `test_adversarial.py::test_rls_configuration_invariants_hold` | the application role is not superuser, cannot bypass RLS, cannot create roles, owns no table; `accounts`, `entries` and `idempotency_keys` have RLS enabled and forced |
| Tenant check present but not bound (v1.7) | `test_tenant_binding.py::test_rls_context_from_the_requested_tenant_leaks_and_membership_binding_closes_it` | with the RLS context set from the URL tenant, a member of ACME reads Globex's accounts (`["globex-ops"]`) although RLS is correctly configured; binding membership first (the real `require_member`) yields 404 for Globex and the ACME rows for ACME |
| Tenant id alone never authorizes (v1.7) | `test_tenant_binding.py::test_a_tenant_id_alone_never_authorizes_a_background_worker` | a worker handed only a tenant id writes into that tenant: the binding must happen where the principal is known, before the id is handed over |

## Defects found while verifying (and fixed before labelling)

- RLS policy cast: after a rolled-back transaction PostgreSQL keeps the custom setting defined with an empty string, so `current_setting('app.tenant_id', true)::uuid` failed with `invalid input syntax for type uuid: ""`. Policies now use `NULLIF(current_setting(...), '')::uuid`.
- Reading an ORM instance after `rollback()` or `commit()` without the tenant context: the row was invisible (RLS) or the refresh raised `MissingGreenlet`. The optimistic rename now returns the row from `UPDATE ... RETURNING` inside the transaction, and the 412 path reads the current version before rolling back.

## Not verified

- Behaviour under PgBouncer or another pooler, other PostgreSQL major versions, or other FastAPI/SQLAlchemy versions.
- Load beyond 50 concurrent in-process requests.
- Running `compose.yaml` on a machine without Docker.

Re-run this record after upgrading any dependency; the label drops to `draft` until it is renewed.
