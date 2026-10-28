# Operations, observability, and production readiness

## SLO-driven operation
Define user-impacting SLIs (availability, correctness, latency, job age), objectives, error budgets, traffic and capacity plans. Alert on symptoms, not every internal error. Trace request across API, jobs, providers with OpenTelemetry where justified; never put PII in span attributes.

## Deployment and rollbacks
Pin images and dependencies; build reproducibly; run unit/integration and policy gates; deploy canary/blue-green/rolling as platform allows. Migrations must be compatible with rolling deployment. Rollback of app does not automatically roll back schema or external side effects.

## Runtime
Liveness should only prove process viability; readiness should prove service can accept traffic without causing cascading restarts. Graceful shutdown drains in-flight requests and worker leases. Apply CPU/memory, request-size, concurrency, DB pool and timeouts. Explicitly configure environment, auth issuer, secrets and allowed origins; do not enable dev behavior by omission.

## Incident response
Runbooks for DB outage, provider auth/JWKS outage, backlog, disk/S3 failures, key rotation, poisoning, tenant isolation incident and failed migration. Record owner, detection, mitigation, rollback, and recovery validation. Practice recovery periodically.

## Cloud decision cues
- Lambda: event-driven, bounded runtime, cold-start/connection considerations; avoid for always-on long stream endpoints.
- ECS/Fargate: durable HTTP/worker containers when ops simplicity preferred.
- Kubernetes: only when platform/team and workload justify operational overhead.
- Async jobs: durable queue or managed worker; do not rely on ephemeral in-process tasks for critical side effects.
- IaC: least privilege, environment isolation, secret references, cost limits and drift detection.
