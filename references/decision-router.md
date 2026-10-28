# Decision router — choose architecture by constraints

## Evidence first
For an existing repository inspect dependency graph, workflows, runtime, database transaction ownership, deployment pipelines, tests, and the incident history before prescribing structure. Distinguish observed facts, hypotheses and proposals. Never reorganize an unrelated module merely to conform to the skill.

## Decision record
For each material choice write: **problem and constraints → options and disqualifiers → selected approach → failure modes → proof/tests → migration/rollback trigger**. A numerical threshold is a heuristic, not universal law.

| Situation | Default | Escalate only when | Avoid |
|---|---|---|---|
| CRUD within one team | modular monolith, modules by business capability | third-party volatility, complex domain, separate release requirements | interfaces for every class |
| Domain rules hard to isolate | pure domain functions + selective ports | variants/providers need substitution | wholesale hexagonal rewrites |
| Read scalability | query optimization, indexes, paging | measured traffic bottleneck | CQRS for cosmetic separation |
| Write contention | unique constraints + optimistic compare-and-swap | hot rows/high contention justify row lock | checking-then-writing in separate transactions |
| Many bounded contexts | modular monolith with public facades | operational/team criteria and observability readiness | microservice per table |
| Async event delivery | transactional outbox + idempotent consumer | decoupling needed, eventual consistency accepted | DB commit then untracked publish |
| Background work | durable queue with retries if critical | execution must survive process restart | FastAPI BackgroundTasks for durable jobs |
| Multi-tenant data | org-scoped queries and authorization, explicit tenant context | RLS for defense in depth, tested fail closed | trusting a tenant ID from client |
| File upload | direct-to-object-store with narrowly scoped signed URL | large files, virus scan, retention, audit | carrying large binaries through DB/API |

## Architecture styles
- Clean/Hexagonal/Onion: variations of dependency direction, not a mandate to generate more layers. Pick ports only around a meaningful volatility or testing seam. Distinguish domain entities from transport schemas and ORM rows where invariants justify it.
- Vertical slices: organize end-to-end features by use case; compatible with modules and can coexist with service/repository for complex areas.
- CQRS: separate *models* only if read/write requirements measurably diverge; separate infrastructure is a later decision. Describe projection lag and reconciliation.
- Event sourcing: only with strong audit/replay requirement, schema evolution ownership, and projection/recovery investment; ordinary append-only audit logs are not event sourcing.
- Microservices: independent deployability must include distributed data ownership, deploy-time compatibility, tracing, incident ownership, and budget for network failures.

## Project review protocol
Inventory the high-risk paths: authentication, permissions, data boundaries, migration, transaction ownership, retries, external dependencies, and logging. Prefer small fixes that can be tested and rolled back. Check that imported types, background jobs, and CLI paths do not accidentally bypass application invariants.
