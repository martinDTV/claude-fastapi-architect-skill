# Python async, concurrency, and resource safety

## Diagnose execution model
`async def` is appropriate for awaitable nonblocking I/O. Blocking sync DB/HTTP/filesystem/CPU work in the event loop causes cross-request latency; move to a thread or process when justified and measure. Sync route handling has worker/threadpool limits. Do not assume async accelerates CPU-bound tasks.

## Lifespan and resources
Create clients, pools and consumers in the app lifespan, close deterministically, and avoid import-time connections. Per-request session isolation; no shared AsyncSession across concurrently scheduled tasks. Bound pool sizes, timeouts, concurrency and queue depths from measured capacity.

## Cancellation and deadlines
- Apply caller deadline and downstream timeouts; understand timeout, retry and cancellation semantics at each layer.
- Avoid suppressing cancellation; if compensating cleanup is required use `finally` and explicit bounded shield with justification.
- Retrying a non-idempotent write requires an application idempotency contract; transport retries alone are unsafe.
- Structured concurrency: group child tasks with cancellation ownership; orphan tasks and swallowing exceptions are failure modes.
- Backpressure: use semaphores, bounded queues, 429/503, concurrency limits, and load shedding rather than unbounded `create_task`.

## Database concurrency
- Pick isolation level by invariant. Unique constraint or atomic UPDATE often outperforms broad row locking.
- `SELECT ... FOR UPDATE` for tightly scoped transactions when necessary; always document lock ordering and deadlock strategy.
- Optimistic concurrency on a monotonic version prevents lost updates; demonstrate with two independent DB sessions.
- Avoid holding transactions during external API calls. Use outbox or state machine for cross-system work.

## Performance review
Measure p50/p95/p99, event-loop lag, connection pool wait, memory, N+1 queries, DB plans, cache hit/miss, and queue age. Use load tests with realistic payloads and access controls; optimize the measured bottleneck, not hypothetical endpoints.

## Lost updates, shared sessions and how to prove them (v1.4)

Static analysis can point at a read-modify-write (`account.balance += x` after a load) or at a session handed to several tasks, but it cannot show that two requests ever overlap. Treat `TX_READ_MODIFY_WRITE` and `SA_SESSION_SHARED_CONCURRENTLY` as questions and answer them with a test on a real database:

1. Open two independent sessions (or two connections). Load the same row in both.
2. Apply the update in each and commit both. With plain read-modify-write the second commit silently overwrites the first.
3. Repeat after the fix and assert the final value reflects both writers, or that one of them receives a conflict.

`tests/integration/test_postgres_properties.py` in the skill repository runs exactly this recipe against a throwaway PostgreSQL cluster and shows `SELECT ... FOR UPDATE` serialising the writers. Choose the mechanism by the invariant, not by habit:

| Situation | Mechanism | Trade-off |
|---|---|---|
| Counters, balances, stock | Atomic `UPDATE ... SET x = x + :d` with a `CHECK` constraint | No lock held across application code; no lost update possible |
| User-edited records | `version_id_col` or `If-Match`/ETag, retry or 412 on conflict | Client participates; no blocking |
| Short critical sections on hot rows | `SELECT ... FOR UPDATE` inside one transaction, locks in a fixed order | Blocking; deadlocks if the order varies; never across external calls |
| "Create if absent" | Unique constraint plus `ON CONFLICT` | Database enforces it under any concurrency |
| Cross-system work | Outbox table, worker, idempotent consumer | Eventual consistency, explicit |

Isolation levels are a last resort after measuring; `REPEATABLE READ`/`SERIALIZABLE` turn lost updates into serialization failures that the application must retry. Retry only idempotent operations, with bounded attempts and jitter, and only on transient errors (serialization failure, deadlock detected, connection reset). A commit inside a loop or an HTTP call inside `session.begin()` extends the window for all of the above; the auditor reports both (`TX_COMMIT_IN_LOOP`, `TX_EXTERNAL_CALL_IN_TRANSACTION`).

### Indirect blocking and request-scoped resources (v1.5)
A synchronous helper called from `async def` blocks the loop exactly like a direct call; the auditor follows resolved project calls to a bounded depth and reports the chain (`ASYNC_BLOCKING_CALL_INDIRECT` with `call_path`). Offload the helper (`anyio.to_thread.run_sync`), make the chain async, or declare the route with plain `def`. A request-scoped `AsyncSession` handed to `BackgroundTasks.add_task` or `asyncio.create_task` outlives its dependency's teardown (`BG_REQUEST_SCOPED_RESOURCE`): pass identifiers and create a session inside the task. Shared `httpx.AsyncClient` instances are fine (designed for concurrent reuse); shared sessions are not.
