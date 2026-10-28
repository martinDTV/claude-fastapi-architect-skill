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
