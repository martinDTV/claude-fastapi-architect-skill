# Durable workflows, messaging and recovery

## Trigger
Queue consumers, Redis Streams, SNS/SQS, Kafka, transactions plus notifications, long-running jobs, sagas, deduplication, webhook delivery.

## Failure matrix before choosing a broker
| Failure | Required design |
|---|---|
| Commit succeeded, publish failed | Transactional outbox with relay and replay |
| Published but ack lost | At-least-once delivery; idempotent consumer |
| Worker crashed mid-handler | Durable claim/visibility timeout, heartbeat or lease, safe retry |
| Messages reordered | Per-entity version, ordering key or commutative processing |
| Poison message | Retry budget, dead-letter queue, triage and redrive policy |
| Remote call timed out | Unknown outcome; reconciliation/read-back before repeat when non-idempotent |
| Deployment during work | Graceful drain and resumable operation state |
| Tenant revoked while queued | Re-authorize at execution for sensitive actions |

## Outbox design
Inside the same DB transaction: domain mutation + outbox row (event ID, aggregate ID, version, type, schema version, timestamp, minimal payload, tenant ID). A separate relay publishes pending rows and marks delivery. Crash before marking means duplicate publish: consumers must deduplicate. Avoid claiming exactly-once delivery without a tightly defined transactional boundary.

## Inbox/idempotent consumption
Persist `(consumer, message_id)` or a business operation key with the state transition atomically. Replayed events return prior outcome. Never simply cache IDs in volatile memory for financial or clinical effects.

## Saga guidance
Use orchestration for multi-step workflows with explicit compensation rules and visibility; choreography for low-coupling reactions. Define which operations are irreversible, which are compensated, and who resolves manual exceptions. A refund is not an automatic inverse of a payment if settlement and fees occurred.

## Job API
POST returns 202 + operation ID and status URL when completion is not immediate. Status includes pending/running/succeeded/failed/cancel_requested/interrupted when applicable; define retention and authorization. Each job needs bounded attempts, retryable vs terminal errors, deadlines, cancellation semantics and cleanup.

## Operational evidence
Metrics: oldest outbox age, publish lag, consumer lag, retry rate, DLQ depth, lease-expired jobs, duplicate rate, failure age. Test worker kill at each step between commit/publish/ack. Redrive procedures need safeguards for schema evolution and tenant ownership.

## Decision template
Document broker capability assumptions, at-least-once semantics, ordering requirements, data sensitivity, failure classes, dedup store, dead-letter retention, replay tooling, reconciliation job, ownership, SLO and rollback plan.

## What the auditor reports about dual writes (v1.6)
`REL_DUAL_WRITE_WITHOUT_OUTBOX` fires when one function commits a database transaction and publishes through a producer/broker/queue that is a library object or project code that reaches the network, with no outbox or inbox table in sight. A project-owned `jobs.enqueue` that inserts into a table with the same session, or an in-process dispatcher that writes an `events` row, is not a dual write and is not reported: the atomicity problem starts where the message leaves the database. The relay that later publishes from the table is where at-least-once delivery and idempotent consumers (inbox) matter; consumer-side dedup detection remains on the backlog.
