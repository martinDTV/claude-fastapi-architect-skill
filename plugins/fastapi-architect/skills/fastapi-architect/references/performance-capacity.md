# Performance, capacity and cost engineering

## Trigger
High latency, memory growth, pool exhaustion, too many workers, expensive infrastructure, throughput goals, load tests or scaling decisions.

## Measurement first
Write a workload model: peak/concurrent users, request mix, payload distribution, expected writes, burstiness, external dependencies, region/network, hard SLO and cost ceiling. Record idle and saturation profiles. Benchmark reproducibly before/after with comparable hardware and data sizes.

## Request budget
Decompose p95/p99 end-to-end into gateway, queueing, application, DB, external API, serialization, response transmission. Prioritize queueing and slow dependencies before micro-optimizing Python. Track latency histograms by route (bounded labels), error categories and request rate. Do not attach raw user IDs or tenant IDs as metric labels.

## FastAPI/ASGI
- Instrument event-loop lag and blocking calls. `async def` improves concurrency for I/O-bound tasks but not CPU throughput.
- Profile sync threadpool exhaustion, uvicorn worker count, restarts, GC and memory pressure.
- Understand multiple-process impact on DB connections and in-memory caches. If workers x pool_size exceeds DB limits, reduce or pool externally.
- Avoid shared mutable module-level state. For streaming, bound memory and per-client resources.

## PostgreSQL
- Inspect EXPLAIN (ANALYZE, BUFFERS) on realistic anonymized datasets; verify indexes match predicate plus ORDER BY, tenant partition, selective conditions.
- Bound pagination. Prefer cursor/keyset for deep scroll with stable tie-breaker, not large OFFSET without a purpose.
- Batch loads to eliminate N+1; inspect ORM loading strategies and transaction lifetime.
- Use optimistic concurrency, unique constraints, or locks to protect invariants; performance changes must not weaken correctness.

## Caching
Only cache after specifying key, owner, TTL, invalidation, sensitivity, negative caching, stampede policy and behavior on miss/outage. Caches are not authorization; revocation latency matters. Prefer short TTL for permission snapshots or event invalidation with fallback to authoritative checks.

## Load testing
Specify open-loop vs closed-loop model, warm-up, ramp and soak; collect p50/p95/p99, error and saturation. Include cold-start, queue backlog, slow DB, dependency timeouts, noisy neighbor and tenant skew. Abort if requests trigger destructive operations on real resources.

## Scale decision
Vertical scaling if capacity bottleneck is CPU/memory and easy to fix. Horizontal scaling if stateless request concurrency benefits. Dedicated worker for heterogeneous CPU/load. New microservice only if independent deployment/scaling ownership justifies network and consistency costs.
