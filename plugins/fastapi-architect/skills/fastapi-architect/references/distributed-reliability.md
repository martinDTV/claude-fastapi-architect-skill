# Distributed systems and durable work

## Choose the smallest reliable mechanism
A local function call beats a network hop when data and deployment ownership have not been separated. Do not share service databases. Design eventual consistency explicitly where a workflow spans data owners.

## Transactional outbox/inbox
- Within the business DB transaction, persist state change and an outbox event; a separate worker publishes with at-least-once delivery semantics.
- Consumers maintain an inbox/deduplication key scoped by event identifier; use idempotent side effects. Never claim exactly-once end-to-end delivery without a specific bounded proof.
- Define ordering partition key, retry schedule, dead-letter/quarantine, poison-message handling, event versioning and replay/rebuild procedure.

## Sagas and consistency
Use orchestration or choreography based on visibility/control. Explicit states, compensations (not always inverse operations), timeouts, human intervention, failure recovery and reconciliation are essential. Prefer a single DB transaction when possible.

## Webhooks and integrations
Verify signature on raw body and timestamp/replay window. Ack promptly only after durable acceptance; retries may duplicate and reorder. Protect against SSRF, DNS rebinding, open redirects, huge bodies and unbounded retry storms. Apply provider-specific quotas, circuit breakers, bulkheads, and exponential backoff with jitter for retryable errors.

## Durable worker lifecycle
Explicit states (queued, leased/running, succeeded, failed, dead/cancelled), heartbeat, lease expiry/reaper, attempt counter, idempotent re-execution, visibility timeout, graceful shutdown and partial-progress checkpoints. Distinguish retriable vs terminal failures. Test worker crash after committing business state but before acknowledging work.

## Extraction readiness
Before separating microservices prove: deploy independence, per-service data ownership, service auth, traffic routing, contract evolution, tracing, secrets, on-call ownership, and rollback. Use strangler migration and dual-read/reconciliation only with a written consistency plan.
