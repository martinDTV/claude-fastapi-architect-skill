# tenant-ledger — FastAPI Architect reference implementation

Label: **`verified`** (see `VERIFICATION.md` for the recorded run). A small, complete multi-tenant ledger that demonstrates the decisions the skill recommends, each one proven by a test against a real PostgreSQL:

| Decision | Where | Proven by |
|---|---|---|
| Stage 1 modular monolith: modules by capability, router → service → repository → models | `src/ledger/modules/{tenants,ledger}` | the test suite exercises every layer through HTTP |
| Engine and `async_sessionmaker` created in the lifespan, one `AsyncSession` per request | `core/db.py`, `main.py` | `test_authz.py`, every request |
| Authorization per resource: membership binds the tenant in the path to the principal; owners only for deletes; 404, not 403, for foreign tenants | `modules/tenants/dependencies.py` | `test_authz.py` |
| Multi-tenancy with PostgreSQL row-level security: non-owner application role, `FORCE ROW LEVEL SECURITY`, transaction-local `set_config('app.tenant_id', ..., true)`, fail-closed policies | `alembic/versions/0002_rls.py`, `core/db.py` | `test_rls.py` (direct SQL as the app role, with and without context) |
| Atomic balance changes (`UPDATE ... SET balance = balance + :d ... RETURNING`) and a `CHECK` constraint instead of read-modify-write | `modules/ledger/repository.py` | `test_concurrency.py`: 50 concurrent deposits, no lost update; overdrafts rejected under concurrency |
| Optimistic concurrency with `ETag` / `If-Match` (428 without, 412 when stale) | `modules/ledger/service.py`, `router.py` | `test_concurrency.py` |
| Idempotency keys for movements: a replayed or concurrent duplicate applies once | `modules/ledger/service.py` | `test_idempotency.py` |
| One commit per use case; failure after the balance update rolls everything back | `modules/ledger/service.py` | `test_rollback.py` |
| Alembic migrations with a naming convention; expand step adds RLS and grants; downgrade implemented | `alembic/` | `test_migrations.py`: empty database to head, previous revision to head, downgrade, `alembic check` |
| RFC 9457 Problem Details for domain and validation errors | `core/errors.py` | `test_authz.py` |
| Anti-patterns reproduced next to their fixes: lost update via read-modify-write; `MissingGreenlet` from an implicit lazy load | `modules/ledger/antipatterns.py` (tests only) | `test_antipatterns.py` |

## Not production authentication

Requests identify themselves with the `X-Demo-User` header (`modules/tenants/security.py`). This exists only so the example can show authorization, isolation and concurrency without an identity provider. Replace it with OIDC/JWT validation before any real use; see `references/authz-multitenancy.md` and `references/identity-threat-models.md` in the skill.

## Run the tests (no setup beyond PostgreSQL binaries)

The suite (30 tests) starts a throwaway PostgreSQL cluster with `initdb`/`pg_ctl` in a temporary directory, runs the migrations, and tears it down. `tests/test_adversarial.py` reproduces, on that cluster, the scenarios the static auditor can only suspect: a shared session, a request-scoped session handed to a task, cancellation by timeout, a lost response retried with and without an idempotency key, and the RLS configuration invariants; `tests/test_tenant_binding.py` shows a tenant check that is present but not bound to the principal, and the membership binding that closes it. It needs the PostgreSQL client and server binaries on `PATH` (Homebrew `postgresql@16`, Debian `postgresql-16`) and `uv`.

```bash
cd plugins/fastapi-architect/examples/tenant-ledger
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests alembic && uv run ruff format --check src tests alembic
```

To run against your own isolated database instead (never a shared one: the suite creates databases and roles and runs migrations up and down):

```bash
export LEDGER_TEST_OWNER_URL=postgresql+asyncpg://owner@host:5432/ledger_test
export LEDGER_TEST_APP_URL=postgresql+asyncpg://ledger_app@host:5432/ledger_test
uv run pytest -q
```

## Run the application

```bash
docker compose up -d                      # PostgreSQL 16 on localhost:5433 with the ledger_app role
cp .env.example .env
uv run alembic upgrade head               # as ledger_owner
uv run uvicorn ledger.main:app --reload   # connects as ledger_app
```

Then, with `-H "X-Demo-User: alice"` (create the user first: `INSERT INTO users (id, username) VALUES (gen_random_uuid(), 'alice')` as the owner):

```bash
curl -s -X POST localhost:8000/api/v1/tenants -H "X-Demo-User: alice" -H "content-type: application/json" -d '{"slug":"acme","name":"ACME"}'
curl -s -X POST localhost:8000/api/v1/tenants/<tenant_id>/accounts -H "X-Demo-User: alice" -H "content-type: application/json" -d '{"name":"main"}' -i
curl -s -X POST localhost:8000/api/v1/tenants/<tenant_id>/accounts/<account_id>/deposits -H "X-Demo-User: alice" -H "Idempotency-Key: k1" -H "content-type: application/json" -d '{"amount":100}'
curl -s -X PATCH localhost:8000/api/v1/tenants/<tenant_id>/accounts/<account_id> -H "X-Demo-User: alice" -H 'If-Match: "2"' -H "content-type: application/json" -d '{"name":"savings"}'
```

## Audit it

```bash
python3 ../../../../scripts/audit_fastapi.py . --format markdown
```

The only findings are the seven deliberate ones in `modules/ledger/antipatterns.py` (`TX_READ_MODIFY_WRITE`, `SA_ASYNC_LAZY_LOAD_RISK`, `SA_SESSION_SHARED_CONCURRENTLY`, `BG_REQUEST_SCOPED_RESOURCE`, `SEC_RLS_CONTEXT_FROM_REQUEST`, and twice `TX_COMMIT_INSIDE_BEGIN_BLOCK` since v1.8), which `tests/test_antipatterns.py`, `tests/test_adversarial.py` and `tests/test_tenant_binding.py` reproduce and fix, plus one informational note on the RLS migration's dynamic SQL. The `contracts` block of the report lists every route with its authentication and authorization kinds; `.audit-fastapi.json` declares `/health` as public on purpose, because a health-check naming convention is never treated as a declaration. `--exclude-path "*/antipatterns.py"` yields a clean report.

## Layout

```
src/ledger/
├── main.py                      # create_app(), lifespan (engine + session factory on app.state)
├── core/                        # config, db (Base, session dependency, tenant context), errors (Problem Details), security (DEMO auth)
├── api/router.py                # /api/v1 aggregation, /health
└── modules/
    ├── tenants/                 # tenants, users, memberships; membership and owner dependencies
    └── ledger/                  # accounts, entries, idempotency keys; repository, service, router, antipatterns (tests only)
alembic/versions/0001_initial.py, 0002_rls.py
tests/                           # ephemeral PostgreSQL, API, RLS, concurrency, rollback, idempotency, migrations, anti-patterns
```

Known limitations are listed in `LIMITATIONS.md`.
