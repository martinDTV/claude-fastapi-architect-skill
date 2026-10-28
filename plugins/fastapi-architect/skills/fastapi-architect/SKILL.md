---
name: fastapi-architect
description: Architecture, project-structure and code-standards guide for Python backends built with FastAPI. Use this skill whenever the user is creating, structuring, refactoring, reviewing or scaling a FastAPI project, including choosing a folder layout, adding a new module or feature, deciding between a monolith, a modular monolith or microservices, splitting a codebase or extracting a service, setting up SQLAlchemy and Alembic, testing, configuration, authentication, Docker or CI, even if they never say the word "architecture". Also use it when the user asks whether a FastAPI codebase is too big, too messy, or "should be microservices".
license: MIT
---

# FastAPI Architect

Guide for building FastAPI backends whose architecture is **right-sized**: as simple as today's known requirements allow, with seams that keep tomorrow's changes cheap.

The most common failure in backend projects is not too little architecture, it is the wrong amount. A CRUD API wrapped in five layers of interfaces is as costly as a 40-endpoint `main.py`. Your job is to pick the level that fits, explain why in a few lines, and apply it consistently.

## Workflow for every task

1. **Understand the context before writing code.** In an existing repo, read `pyproject.toml`, `CLAUDE.md` or `AGENTS.md` if present, the folder tree (`find src -name "*.py" | head -100` or similar), the app entry point, the DB setup and the tests. Note the conventions already in use.
2. **Determine the stage** (next section). For an existing project, identify the stage it is in and the stage it should be in.
3. **State the decision in two or three lines** before implementing: which stage, why, and what you deliberately left out. For decisions that are expensive to reverse (database, sync vs async, splitting a service, auth provider), write an ADR using `assets/adr-template.md`.
4. **Implement** following the structure and standards below.
5. **Verify** with the project's tooling. With the recommended setup: `uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run lint-imports && uv run pytest`. Do not report a task as done while any of these fail.

Ask a clarifying question only when the answer would change the decision (for example: number of teams, deployment target, expected load, database already chosen). Otherwise make a reasonable assumption, state it, and continue.

Respect existing conventions. If the project already has a coherent structure, follow it even if it differs from this guide. Do not rewrite a codebase that was not asked to be rewritten; propose an incremental migration instead and apply only the part the task needs.

## Choosing the stage

Pick the **lowest** stage that satisfies the requirements the user actually has. Do not build for hypothetical scale.

| Stage | When | Shape |
|---|---|---|
| **0. Prototype** | Spike, internal script, demo, under ~5 endpoints, one developer, no long-term maintenance expected | Single package, a few files. No repository or service layers. |
| **1. Modular monolith** (default) | Anything going to production or expected to live more than a few months | One deployable. Code organized by business module, each with router, schemas, service, repository and models. |
| **2. Modular monolith with ports and adapters** | Specific modules with rich business rules, several external integrations, or adapters that genuinely vary (payment providers, storage backends) | Same as Stage 1, but those modules add a pure `domain/` and `Protocol` ports. Other modules stay Stage 1. |
| **3. Distributed services** | Only when the hard criteria in "From monolith to microservices" are met | Several deployables, each internally a Stage 1 or 2 app, owning its own data. |

Signals that push a project up a stage:
- 0 → 1: a second developer joins, it is deployed for real users, there is a database with real data, or there are more than a handful of endpoints.
- 1 → 2 (per module): business rules are hard to test through HTTP, the module talks to two or more external systems, or an adapter needs to be swappable.
- 2 → 3: see the next section. Size alone is never enough.

Full folder trees for each stage, with what goes in every file, are in `references/project-structure.md`. Read it when scaffolding a project or reorganizing one.

## Stage 1: the modular monolith (default)

### Modules are business capabilities, not technical layers

Organize by what the system does (`customers`, `orders`, `billing`), never by file type at the top level (`routers/`, `models/`, `services/`). A by-type layout scatters every feature across the tree and makes future extraction impossible without archaeology.

```
src/app/
├── main.py            # create_app(), lifespan, router registration
├── core/              # config, db, logging, security, base exceptions
├── api/               # top-level router aggregation, exception handlers
└── modules/
    └── customers/
        ├── router.py        # HTTP only: parse, call service, return schema
        ├── schemas.py       # Pydantic request/response models
        ├── service.py       # business rules and transaction boundary
        ├── repository.py    # queries, no business rules, never commits
        ├── models.py        # SQLAlchemy ORM models
        ├── dependencies.py  # Depends() wiring and Annotated aliases
        ├── exceptions.py    # domain exceptions, no HTTP knowledge
        └── public.py        # optional facade other modules may import
```

### Dependency direction inside a module

`router → service → repository → models`. A lower layer never imports a higher one. `schemas` can be used by router and service. Services raise domain exceptions; they never raise `HTTPException` and never import FastAPI. That keeps services callable from workers, CLIs and tests.

### Rules between modules

- A module imports another module **only through its `public.py`** (functions or a small service facade). Never import another module's `models`, `repository` or internal service.
- No module queries another module's tables. If `orders` needs customer data, it calls `customers.public`.
- No foreign-key-driven ORM relationships across module boundaries. Store the ID; fetch through the public API when needed.
- `core/` holds infrastructure only (config, DB session, logging, auth primitives, base exception classes). Business logic never goes in `core/`; a growing `core/` or `utils/` is a smell.

These rules are what make the monolith *modular*. Enforce them mechanically with import-linter (configuration in `references/architecture-decisions.md`), not with code-review goodwill.

## Stage 2: ports and adapters, per module

Apply this only to modules that need it. Inside such a module:

```
modules/payments/
├── domain/            # entities and value objects as dataclasses, pure functions, no I/O
├── ports.py           # typing.Protocol interfaces (PaymentGateway, PaymentRepository)
├── adapters/          # concrete implementations (stripe.py, sqlalchemy_repository.py)
├── service.py         # orchestrates domain + ports
└── router.py, schemas.py, dependencies.py, exceptions.py, public.py
```

Use `typing.Protocol` for ports, not ABCs: structural typing keeps adapters decoupled and test fakes trivial. Wire implementations with FastAPI's `Depends`; a DI container library is unnecessary for a FastAPI app and adds indirection. Do not create interfaces with a single implementation and no test double; that is ceremony, not architecture.

## From monolith to microservices

Extracting a service is justified only when **at least two** of these hard criteria are true, and they are true now, not someday:

1. **Independent scaling**: one module's load profile is radically different (CPU-bound processing, bursty ingestion) and scaling the whole monolith for it is measurably expensive.
2. **Independent deployment**: separate teams are blocked by each other's release cycles on a regular basis.
3. **Team ownership**: a distinct team (roughly 4 to 8 people) will own the service end to end, including on-call.
4. **Isolation requirement**: compliance, security or fault-isolation boundaries (payment card data, a flaky third-party integration that must not take the core down).
5. **Different runtime needs**: a different language, hardware (GPU) or execution model that cannot live in the same process.

Prerequisites, all required before extracting:
- The module already respects the boundary rules above and import-linter passes.
- The module owns its tables and nothing else reads them.
- CI/CD can deploy services independently, and there is centralized logging, tracing and metrics.
- There is a plan for data consistency across services (transactional outbox, idempotent consumers, sagas instead of distributed transactions).

Not reasons to split: "the codebase is big", "microservices are best practice", "to use Kubernetes", a single team under ~8 developers, or hoping that splitting will fix a messy codebase. Splitting a messy monolith produces a distributed monolith: every downside of both.

When the user asks to split and the criteria are not met, say so plainly, list which criteria are missing, and propose the step that captures most of the benefit: enforce module boundaries in the monolith. If they still want to proceed, follow the extraction playbook in `references/architecture-decisions.md` properly rather than half-doing it.

## Code standards (non-negotiable)

Each rule exists because breaking it causes a recurring, expensive class of bug. Details and examples are in `references/code-standards.md`.

- **Tooling**: Python 3.12+, `uv` for dependencies and the lockfile, `ruff` for formatting and linting, `mypy --strict` with the pydantic plugin, `pytest`, `import-linter`, pre-commit. Templates in `assets/`.
- **App factory and lifespan**: build the app in `create_app()`. Create engines and clients in a `lifespan` context manager and store them on `app.state`. Never use `@app.on_event` (deprecated) or create connections at import time.
- **Typed dependencies**: use `Annotated` aliases (`SessionDep = Annotated[AsyncSession, Depends(get_session)]`) instead of repeating `Depends(...)` defaults.
- **Async correctness**: use `async def` only when everything awaited inside is non-blocking. If a path operation calls a blocking library, declare it with plain `def` (FastAPI runs it in a threadpool) or offload with `anyio.to_thread.run_sync`. A single blocking call inside `async def` stalls every request on that worker.
- **Schemas**: separate `XCreate`, `XUpdate` and `XRead` models; never accept or return ORM objects directly; declare the return type of every path operation so FastAPI validates and filters the response.
- **Errors**: domain exceptions in `exceptions.py`, mapped to HTTP status codes by global handlers in `api/errors.py`, returned as RFC 9457 Problem Details. No `try/except` that converts to `HTTPException` in every route.
- **Configuration**: one `Settings` class with `pydantic-settings`, read through a cached `get_settings()`. No `os.getenv` scattered through the code. Secrets as `SecretStr`.
- **Database**: SQLAlchemy 2.0 typed models (`Mapped[...]`), one session per request via a dependency, transaction committed by the service at the end of a write use case, repositories only `add`/`flush`. Alembic for every schema change, with a naming convention on the metadata. Details in `references/data-layer.md`.
- **API design**: version prefix (`/api/v1`), plural resource nouns, correct status codes (201 on create, 204 on delete), pagination on every list endpoint, explicit `operation_id`-friendly function names.
- **No print, no bare except, no mutable defaults, no relative imports, no business logic in routers.**

## Reviewing an existing FastAPI codebase

Report findings ordered by impact, and say which stage the project is in versus which it should be in. Check, in this order:

1. Blocking I/O inside `async def` (requests, psycopg2, `time.sleep`, sync SDKs).
2. Sessions or connections created at import time or shared globally across requests.
3. Business logic in routers; services raising `HTTPException`.
4. Cross-module imports of models or repositories; queries on another module's tables.
5. ORM objects returned without a response schema (data leaks, lazy-load errors).
6. Missing migrations or `create_all()` used in production.
7. Configuration and secrets read ad hoc; CORS `"*"` combined with credentials.
8. Missing tests on business rules; tests that hit real external services.
9. Over-engineering: interfaces with one implementation, DI containers, generic base repositories that every query works around.

For each finding give the location, why it matters, and the concrete fix. Fix only what the user asked to fix; list the rest.

## Reference files

Read the file that matches the task. Do not load all of them by default.

| File | Read when |
|---|---|
| `references/project-structure.md` | Scaffolding a new project, reorganizing folders, adding a module |
| `references/architecture-decisions.md` | Choosing a stage, enforcing boundaries, splitting a monolith, inter-service communication |
| `references/code-standards.md` | Writing routers, schemas, services, dependencies, error handling |
| `references/data-layer.md` | SQLAlchemy models, sessions, transactions, repositories, Alembic, query performance |
| `references/testing.md` | Writing or fixing tests, test database setup, test structure |
| `references/security-and-config.md` | Settings, secrets, authentication, authorization, CORS |
| `references/operations.md` | Logging, tracing, health checks, Docker, deployment, workers |

Assets to copy into projects: `assets/pyproject.toml`, `assets/Dockerfile`, `assets/pre-commit-config.yaml`, `assets/adr-template.md`.


## Extended decision and implementation references (v2)

Load only the relevant reference for each task; preserve the existing architecture-stage selection above.

| Reference | Trigger |
|---|---|
| `references/decision-router.md` | Choosing architecture, reviewing a system, evaluating CQRS/DDD/microservices |
| `references/api-contracts.md` | API contracts, pagination, sort, ETag, idempotency, compatibility |
| `references/authz-multitenancy.md` | Authentication, authorization, OIDC/JWKS, RLS, tenant isolation, security |
| `references/async-concurrency.md` | Async, cancellation, timeouts, lost updates, concurrency, load |
| `references/distributed-reliability.md` | Webhooks, outbox/inbox, workers, microservices, sagas, retries |
| `references/database-evolution.md` | PostgreSQL, migrations, indexing, backups, recovery |
| `references/testing-matrix.md` | Unit/integration/e2e/contract, adversarial tests, skill evaluation |
| `references/operations-production.md` | CI/CD, SLO, observability, capacity, incidents, cloud deployment |

Do not load all references automatically. Preserve existing repo conventions and project ADRs. Treat defaults as heuristics, not universally binding rules. State what has actually been verified; do not claim a test ran without running it.
