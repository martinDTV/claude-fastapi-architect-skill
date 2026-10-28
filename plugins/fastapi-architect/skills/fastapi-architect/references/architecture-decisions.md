# Architecture decisions

## Contents
- Decision principles
- Stage decision guide, with examples
- Enforcing module boundaries with import-linter
- Communication between modules inside the monolith
- From monolith to microservices: readiness assessment
- Extraction playbook (strangler fig)
- Communication between services
- Data ownership and consistency
- Anti-patterns
- Writing an ADR

## Decision principles

1. **Optimize for change, not for imagined scale.** The architecture should make the next likely change cheap. Most systems never reach the scale people design for; all of them change.
2. **Reversible decisions are made fast; irreversible ones get an ADR.** Folder names are reversible. The database, the auth model and service boundaries are not.
3. **Boundaries before distribution.** A boundary that is not enforced in one process will not survive being split across a network.
4. **Every layer must pay rent.** If a layer only forwards calls, remove it. If an interface has one implementation and no test double, remove it.
5. **Prefer boring, well-documented tools** that the team already knows, unless a requirement rules them out.

## Stage decision guide

| Situation | Decision |
|---|---|
| Internal tool, 3 endpoints, one developer, maybe thrown away | Stage 0 |
| New SaaS product, small team, will go to production | Stage 1 |
| Existing Stage 1 app adds a payments module with two providers and complex refund rules | Stage 2 for `payments` only |
| A PDF/image processing module needs 10x the CPU of the rest and blocks API latency | First move the work to a background worker inside the monolith. Extract a service only if the worker still needs independent deployment or ownership |
| Two teams of 6 keep blocking each other's releases, boundaries already enforced | Stage 3 candidate: run the readiness assessment |
| Single team of 4, codebase is "too big", lots of cross-imports | Stay in one deployable. Refactor into enforced modules (Stage 1). Splitting now would create a distributed monolith |
| Serverless deployment required (for example AWS Lambda) | Deployment target, not architecture. Keep the Stage 1 layout and run it through an ASGI adapter; split into several functions only along module boundaries and only if cold-start or scaling data justifies it |

Background workers (Celery, arq, Dramatiq, or a queue consumer) are **not** microservices. They are another entry point into the same codebase, sharing modules and calling services through the same public APIs. Prefer this before extraction.

## Enforcing module boundaries with import-linter

Add `import-linter` to the dev group and configure contracts in `pyproject.toml`. Update the module lists whenever a module is added.

```toml
[tool.importlinter]
root_packages = ["app"]

[[tool.importlinter.contracts]]
name = "Modules are independent (only public.py may be imported across modules)"
type = "independence"
modules = [
    "app.modules.customers",
    "app.modules.orders",
]
ignore_imports = [
    # Declare every allowed cross-module dependency explicitly, always to public.py
    "app.modules.orders.service -> app.modules.customers.public",
]

[[tool.importlinter.contracts]]
name = "Layered modules"
type = "layers"
layers = [
    "router",
    "dependencies",
    "service",
    "repository",
    "models",
]
containers = [
    "app.modules.customers",
    "app.modules.orders",
]

[[tool.importlinter.contracts]]
name = "Core does not depend on business modules"
type = "forbidden"
source_modules = ["app.core"]
forbidden_modules = ["app.modules", "app.api"]
```

Run with `uv run lint-imports` in pre-commit and CI. The `ignore_imports` list becomes living documentation of the coupling between modules: when it grows long between two modules, they are probably one module, or the boundary is wrong.

For Stage 2 modules, add a forbidden contract so the domain stays pure:

```toml
[[tool.importlinter.contracts]]
name = "Payments domain is pure"
type = "forbidden"
source_modules = ["app.modules.payments.domain"]
forbidden_modules = ["fastapi", "sqlalchemy", "pydantic", "app.modules.payments.adapters"]
```

(Third-party modules in `forbidden_modules` require `include_external_packages = true` under `[tool.importlinter]`.)

## Communication between modules inside the monolith

- **Synchronous call through `public.py`**: default. Simple, transactional, easy to trace.
- **In-process domain events**: when one module must react to another without the emitter knowing about it (for example, `orders` emits `OrderPlaced`, `notifications` reacts). Use a small dispatcher in `core/events.py`; handlers that do I/O to external systems should enqueue a background job rather than run inline.
- **Shared database transaction across modules**: acceptable inside the monolith when two modules must change together atomically, but each module still writes only its own tables through its own service. If this happens constantly between the same two modules, merge them.

Design module APIs as if they might become remote one day: pass IDs and small DTOs, not ORM objects; avoid chatty call patterns (N calls in a loop).

## From monolith to microservices: readiness assessment

Score each item for the candidate module. Extraction is justified only with at least two hard criteria **and** all prerequisites.

Hard criteria (need at least two):
- [ ] Independent scaling need, measured (not guessed) as costly to satisfy in the monolith
- [ ] Release coupling between teams that recurs in practice
- [ ] A dedicated team will own build, deploy and on-call
- [ ] Compliance, security or fault-isolation boundary
- [ ] Different runtime, language or hardware requirement

Prerequisites (need all):
- [ ] Module boundary enforced by import-linter, few declared cross-module imports
- [ ] Module owns its tables; no other module reads or joins them
- [ ] Independent CI/CD pipeline possible
- [ ] Centralized logs, distributed tracing and metrics in place
- [ ] Consistency strategy chosen (outbox, idempotency, sagas)
- [ ] Contracts defined (OpenAPI for HTTP, versioned schemas for events)

If the result is "not ready", report which items fail and what to do first. That report is a valid, complete answer.

## Extraction playbook (strangler fig)

Never big-bang rewrite. Extract one module at a time, keeping the system working at every step.

1. **Harden the boundary in the monolith.** Make import-linter pass with zero ignored imports into the candidate except through `public.py`.
2. **Separate its data.** Move its tables to a dedicated schema (Postgres schema or separate database user). Remove cross-module joins; replace with calls through `public.py`.
3. **Make the public API remote-ready.** Callers depend on a `Protocol`; the current implementation is in-process. Add a second implementation as an HTTP client (with timeouts and retries) behind a setting.
4. **Publish events through a transactional outbox.** Writes and their events are committed in the same transaction; a relay publishes them to the broker.
5. **Deploy the module as its own service**, built from the same code initially, with its own database copy and migration pipeline.
6. **Switch traffic gradually**: flip the setting to the HTTP implementation for a percentage of traffic or one environment at a time; route its external endpoints through the gateway.
7. **Delete the in-process code path** from the monolith once traffic is fully moved and stable.
8. **Write the ADR** recording why, the criteria met, and the rollback plan.

## Communication between services

- **Asynchronous events** (Kafka, RabbitMQ, SNS/SQS, Pub/Sub) for propagating state changes. Consumers must be idempotent; include an event ID and version in every message.
- **Synchronous HTTP or gRPC** only for queries that need an immediate answer. Always set timeouts, bounded retries with backoff and jitter, and circuit breaking for non-critical dependencies. Use a shared `httpx.AsyncClient` created in `lifespan`, never one per request.
- **Avoid synchronous chains deeper than two hops.** Each hop multiplies latency and failure probability. Replicate the data needed via events instead.
- **Version contracts.** Additive changes only within a version; breaking changes get a new version run side by side until consumers migrate.

## Data ownership and consistency

- One service, one database (or schema with exclusive credentials). No other service reads it directly.
- No distributed transactions (2PC). Use sagas: a sequence of local transactions with compensating actions on failure, either orchestrated (a coordinator) or choreographed (events).
- Transactional outbox for every state change that emits an event, so the database write and the event cannot diverge.
- Reporting across services goes to a read model or warehouse fed by events, not to cross-database queries.

## Anti-patterns

- **Distributed monolith**: services that must deploy together, share a database, or call each other synchronously in chains.
- **Shared domain library** between services.
- **Entity services** (`user-service`, `address-service`) split by noun instead of by business capability, which guarantees chatty calls.
- **By-type folder layout** (`routers/`, `models/`, `services/` at the top level).
- **Architecture astronautics**: DI containers, abstract base classes with one implementation, generic repositories, CQRS and event sourcing without a concrete requirement.
- **God `utils.py` or `common/` module** accumulating business logic.

## Writing an ADR

Use `assets/adr-template.md`. Store as `docs/adr/NNNN-short-title.md`, numbered sequentially. Write one when a decision is costly to reverse: choosing the stage of the system, database, sync vs async stack, authentication provider, service extraction, messaging technology. Keep it under one page. An ADR is never edited after acceptance; a new ADR supersedes it.

## Boundary checks by the auditor and how they relate to import-linter (v1.4)

The auditor's `architecture` category infers layers from paths (router, service, repository, domain, adapter, core) and the business-modules root (the shallowest `modules/` or `features/` directory with two or more packages), then reports: a service or repository importing FastAPI, a domain module importing infrastructure, a repository importing a service, a module importing another module's internals instead of `public.py`, core importing a business module, and module-level import cycles. The inference is written to the report's `facts` block; `.audit-fastapi.json` can declare `layers` and `modules_root` for projects with other conventions (vertical slices, hexagonal, custom names), which raises the findings' confidence from medium to high.

When the project already has import-linter contracts (`.importlinter`, `setup.cfg`, `pyproject.toml`), the auditor lists them and reports its own boundary findings with one confidence level less. The contracts are the source of truth: run `lint-imports` and do not add parallel rules that contradict them; use the auditor's findings to propose new contracts where none exist. Existing architectures are respected: a hexagonal layout with pure `domain/`, `application/` use cases and `adapters/` produces no findings; microservice repositories without layered directories are left alone; test files never participate in the graph.
