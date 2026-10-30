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
..............................
30 passed in 7.90s            (22 in the initial record; 5 adversarial tests added for v1.6; 2 tenant-binding tests for v1.7; 1 commit-inside-begin test for v1.8)

$ uv run pytest -q -m remediation tests/test_remediation_targets.py            (v1.9 targets, deselected by default)
5 failed in 2.00s             (they fail on the anti-patterns on purpose: begin-block raise, loop shape, dropped insert, swallowed error, non-atomic loop)

$ python3 ../../../../scripts/audit_remediate.py . --authorize-execution --trust fixture --trust-scope . \
    --env-passthrough UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR --timeout 900 \
    --link "src/ledger/modules/ledger/antipatterns.py:176=uv run --frozen --extra dev pytest -q -m remediation tests/test_remediation_targets.py -k test_commit_inside_begin_is_remediated" \
    --link "src/ledger/modules/ledger/antipatterns.py:190=... -k test_commit_per_item_inside_begin_is_remediated" \
    --link "SA_ASYNC_SESSION_NOT_AWAITED=... -k note_without_await" --link "TX_COMMIT_IN_LOOP=... -k deposit_each" \
    --test-command "uv run --frozen --extra dev pytest -q -m 'remediation or not antipattern_demo'" --patch-dir patches --output run.json
project untouched: yes        (digest of the project's Python files identical before and after; one temporary copy per case plus a composed copy)
executor: local (process_hygiene); not provided: network isolation, filesystem confinement beyond the fresh HOME, memory limits, protection against a malicious suite
cases: 4 verified             each in its own copy, each reproducer instrumented with the pytest JUnit adapter (report bound to the run by its nonce),
                              run before (failed) and after (passed) on a throwaway PostgreSQL 16 cluster, with the tests that flipped:
  antipatterns.py:176  TX_COMMIT_INSIDE_BEGIN_BLOCK  remove_commit_inside_begin  1 executed: failed -> passed   flipped test_commit_inside_begin_is_remediated
  antipatterns.py:190  TX_COMMIT_INSIDE_BEGIN_BLOCK  remove_commit_inside_begin  1 executed: failed -> passed   flipped test_commit_per_item_inside_begin_is_remediated
  antipatterns.py:212  SA_ASYNC_SESSION_NOT_AWAITED  add_await                   2 executed: failed -> passed   flipped ..._executes_exactly_once_without_warnings, ..._surfaces_database_errors_after_the_fix
  antipatterns.py:224  TX_COMMIT_IN_LOOP             move_commit_after_loop      1 executed: failed -> passed   flipped test_deposit_each_is_atomic_when_an_amount_is_rejected
composition: verified         the four candidates applied to one more copy (identities and digests recorded); audit clean; every reproducer passes there;
                              regression suite of the corrected code (-m 'remediation or not antipattern_demo') before: 29 passed, 5 failed; after: 34 passed
124 s wall clock, 116 MB peak RSS (six virtual environments synced from the local uv cache, six clusters)
Why the regression suite excludes one test: tests/test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement asserts the
anti-pattern's defective behaviour on purpose (it documents the raise). An earlier composition that ran the default suite failed exactly that test;
it was neither deleted nor silenced: it carries the `antipattern_demo` marker, still runs in the default suite against the anti-pattern code, and
the regression suite of the corrected code is declared without it. The other anti-pattern tests keep passing after the candidates.
(the first delivery's attempt without `--extra dev` ended `rejected`: pytest was not in the copy's environment; with the hardened model that run
 is `inconclusive: infrastructure_error`, and a run whose report is missing is `inconclusive: evidence_missing`)

$ uv run ruff check src tests alembic
All checks passed!
$ uv run ruff format --check src tests alembic
41 files already formatted

$ python3 ../../../../scripts/audit_fastapi.py .          (auditor 1.8.0, schema 1.6)
files scanned: 39  findings: 10 (high 5, medium 4, low 0, info 1)
  alembic/versions/0002_rls.py:32  MIG_RAW_SQL                      info   (dynamic SQL: role name from configuration)
  src/ledger/modules/ledger/antipatterns.py:20   TX_READ_MODIFY_WRITE            medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:29   SA_ASYNC_LAZY_LOAD_RISK         medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:71   SA_SESSION_SHARED_CONCURRENTLY  high   (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:102  BG_REQUEST_SCOPED_RESOURCE      high   (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:142  SEC_RLS_CONTEXT_FROM_REQUEST    medium (deliberate, reproduced in tests)
  src/ledger/modules/ledger/antipatterns.py:176  TX_COMMIT_INSIDE_BEGIN_BLOCK    high   (deliberate, reproduced in tests; v1.8)
  src/ledger/modules/ledger/antipatterns.py:190  TX_COMMIT_INSIDE_BEGIN_BLOCK    high   (deliberate, loop shape, reproduced in tests; v1.8)
  src/ledger/modules/ledger/antipatterns.py:212  SA_ASYNC_SESSION_NOT_AWAITED    high   (deliberate, remediated on PostgreSQL by the tool; v1.9)
  src/ledger/modules/ledger/antipatterns.py:224  TX_COMMIT_IN_LOOP               medium (deliberate per-item loop, remediated on PostgreSQL by the tool; v1.9)
  the six v1.7 fingerprints are identical; the four later findings carry a `remediation` block (status proposed, never applied) that names the recipe
  contracts: 13 routes, authenticated 12, public 1 (/health, declared in .audit-fastapi.json; a naming convention alone would leave it as exposure undetermined), unauthenticated and not public 0, authorization visible 9, authorization unknown 1 (the deliberate unbound route)
$ python3 ../../../../scripts/audit_fastapi.py . --exclude-path "*/antipatterns.py" --ignore MIG_RAW_SQL --fail-on info
exit 0 (no findings)
```

## What each test proves (30 tests plus five deselected remediation targets, all against PostgreSQL)

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
| commit() inside `with session.begin()` (v1.8) | `test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement` | SQLAlchemy 2.0.55 on PostgreSQL: the inner commit closes the block's transaction and the next statement raises `InvalidRequestError` ("Can't operate on closed transaction inside context manager"); the first delta is committed, the second never runs; the loop shape fails on its second iteration; without the inner commit both deltas are visible together after exit. This record corrected the rule's first draft, which claimed the later statements ran in an unmanaged transaction |
| Remediation targets (v1.9, marker `remediation`, deselected by default) | `test_remediation_targets.py` (5 tests) | each fails on its anti-pattern and passes once its recipe has been applied in the case's isolated copy: begin-block statement after the commit, begin-block loop shape, dropped insert executed exactly once without a `never awaited` warning, a foreign-key error that now surfaces (documented behaviour change of `add_await`), and a per-item deposit loop that leaves the balance unchanged when an amount is rejected (atomicity). The tool ran each before and after on a throwaway cluster inside its copy, reading the JUnit report bound to the run; `test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement` carries the `antipattern_demo` marker because it asserts the defect on purpose |
| Tenant id alone never authorizes (v1.7) | `test_tenant_binding.py::test_a_tenant_id_alone_never_authorizes_a_background_worker` | a worker handed only a tenant id writes into that tenant: the binding must happen where the principal is known, before the id is handed over |

## Defects found while verifying (and fixed before labelling)

- RLS policy cast: after a rolled-back transaction PostgreSQL keeps the custom setting defined with an empty string, so `current_setting('app.tenant_id', true)::uuid` failed with `invalid input syntax for type uuid: ""`. Policies now use `NULLIF(current_setting(...), '')::uuid`.
- Reading an ORM instance after `rollback()` or `commit()` without the tenant context: the row was invisible (RLS) or the refresh raised `MissingGreenlet`. The optimistic rename now returns the row from `UPDATE ... RETURNING` inside the transaction, and the 412 path reads the current version before rolling back.

## Not verified

- Behaviour under PgBouncer or another pooler, other PostgreSQL major versions, or other FastAPI/SQLAlchemy versions.
- Load beyond 50 concurrent in-process requests.
- Running `compose.yaml` on a machine without Docker.

Re-run this record after upgrading any dependency; the label drops to `draft` until it is renewed.
