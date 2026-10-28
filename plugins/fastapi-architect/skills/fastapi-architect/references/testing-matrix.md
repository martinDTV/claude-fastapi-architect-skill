# Evidence-based test strategy

## Risk-driven pyramid
- Pure unit tests: invariants, authorization decisions, calculations, state machines.
- Integration: real PostgreSQL for transactions, RLS, unique constraints, migrations, concurrency; provider fakes for deterministic contracts.
- Contract: OpenAPI schema snapshot and compatibility, webhook event variants, producer/consumer compatibility.
- E2E: a few real user workflows in controlled environments, including auth and tenant switch.
- Nonfunctional: k6/Locust load with SLA, dependency downtime, idempotency races, security scans, migration/restore drills.

## Mandatory adversarial examples
1. Two concurrent writes with the same ETag; exactly one succeeds.
2. Cross-tenant RLS query and cross-tenant foreign-key attempt fail closed.
3. Expired/revoked/foreign-client JWT rejected; JWKS rotation and removed key tested.
4. Duplicate/late/reordered webhook cannot duplicate side effects.
5. Worker crash after DB commit can resume without duplicating delivery.
6. List endpoint sort injection and unbounded pagination rejected.
7. Secret and patient data absent from logs, errors, headers, traces.
8. Time-zone DST fold/gap behavior deterministic.
9. New schema with old code and old schema with new code reviewed for rollout safety.
10. Third-party outage does not exhaust API worker pools.

## Evaluation of the skill itself
For each scenario score: architecture right-sizing (0-3), correctness of contracts (0-3), security (0-3), implementability/verification (0-3), avoids unnecessary complexity (0-3). Penalize unsupported claims and invented APIs. Compare baseline against enhanced skill. Keep reproducible fixtures and model outputs; evaluate both task-selection and implementation quality.

## Tooling checks
Format, lint, strict typecheck, import boundaries, pytest, migrations, dependency audit and tested sample snippets. Mark checks not run; a green suite is never proof of security by itself.

## Closing static findings with runtime tests (v1.4)

Each auditor finding states its `verification`. Map it to a test before touching code:

| Finding type | Test that closes it |
|---|---|
| Shared `AsyncSession`, lost update (`SA_SESSION_SHARED_CONCURRENTLY`, `TX_READ_MODIFY_WRITE`) | Two independent sessions on a real PostgreSQL, same row, concurrent or interleaved commits; assert both writes survive or one gets a conflict |
| Session-level `SET` for tenant context (`SEC_RLS_SET_NOT_LOCAL`) | Same connection: set, commit, `SHOW`/`current_setting` in a new transaction; must be reset |
| Tenant id from request (`SEC_TENANT_ID_FROM_REQUEST`) | Member of tenant A requests B's identifier through the route: single, list, search, aggregate; expect 403/404 and no row |
| Blocking call in `async def` (`ASYNC_BLOCKING_CALL`, `SA_SYNC_SESSION_IN_ASYNC_DEF`) | Concurrent request against a trivial endpoint on one worker while the suspect handler runs; measure the trivial endpoint's latency |
| Destructive or locking migration (`MIG_*`) | Rehearse on an empty database and on a production-sized copy with a concurrent writer; run the previous release against the new schema |
| Commit in loop, external call in transaction (`TX_*`) | Inject a failure or a stall and assert the intended partial-state and lock behaviour |

Keep these tests separate from the AST tests: they need infrastructure (an ephemeral PostgreSQL started per test run is enough, as in `tests/integration` of the skill repository) and must never point at a shared or production database.
