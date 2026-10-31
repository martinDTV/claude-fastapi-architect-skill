# Documentation index

The repository root holds the entry point (`README.md`); everything else lives here or inside the plugin, where Claude Code expects it.

## Contractual documents (do not move)

| Document | Purpose |
|---|---|
| [`plugins/fastapi-architect/skills/fastapi-architect/SKILL.md`](../plugins/fastapi-architect/skills/fastapi-architect/SKILL.md) | the skill: workflow, stages, standards, auditor and remediation usage |
| [`plugins/fastapi-architect/skills/fastapi-architect/references/`](../plugins/fastapi-architect/skills/fastapi-architect/references/) | the 28 reference documents the skill routes to (architecture, data layer, security, auditor engine and rules, safe refactoring, design and evolution contracts, …) |
| [`plugins/fastapi-architect/evals/README.md`](../plugins/fastapi-architect/evals/README.md) | the offline evaluation suite (`claude plugin eval` format) |
| [`plugins/fastapi-architect/examples/tenant-ledger/`](../plugins/fastapi-architect/examples/tenant-ledger/README.md) | the verified reference implementation (README, VERIFICATION, LIMITATIONS) |
| [`plugins/fastapi-architect/examples/specs/`](../plugins/fastapi-architect/examples/specs/), [`examples/evolutions/`](../plugins/fastapi-architect/examples/evolutions/) and [`examples/generated/`](../plugins/fastapi-architect/examples/generated/README.md) | the example architecture and evolution contracts (v2.0), the backends generated from them (`inventory-service`, `orders-platform`, `team-notes`, `tenant-projects`), and the [verification evidence](../plugins/fastapi-architect/examples/generated/VERIFICATION.md) of the PostgreSQL runs |

## Architecture records

| Document | Purpose |
|---|---|
| [`architecture/design-engine-v2.md`](architecture/design-engine-v2.md) | why the v2.0 design engine is a declarative contract, a conservative advisor, a pure blueprint and a guarded writer, and how it reuses the audit and remediation engines |

## Guides

| Document | Purpose |
|---|---|
| [`guides/generated-postgresql-projects.md`](guides/generated-postgresql-projects.md) | persistence, Alembic migrations, the generated test suite and the opt-in verification of a generated PostgreSQL backend against a throwaway cluster |
| [`guides/evolving-generated-projects.md`](guides/evolving-generated-projects.md) | discover an existing project, write an evolution contract, preview the plan, approve and apply it with verification in a copy; changing existing modules (1.1), adopting a hand-written project, recovering an interrupted write; what the engine protects |
| [`guides/identity-authorization-and-rls.md`](guides/identity-authorization-and-rls.md) | the five security layers of a schema 1.2 contract (OIDC or API-key grants, principal-tenant binding, per-operation roles, ownership, row-level security), what is generated for each, how to run the generated proofs with two database roles, how to read 401/403/404/503, evolving safely, limits |

## Release notes

| Version | Title |
|---|---|
| [v2.0](releases/RELEASE-NOTES-v2.0.md) | Architecture & Engineering Foundations (increments 1 to 5: design engine, PostgreSQL, evolution, adoption, identity and tenant isolation) |
| [v1.9](releases/RELEASE-NOTES-v1.9.md) | Verified Remediation & Safe Refactoring |
| [v1.8](releases/RELEASE-NOTES-v1.8.md) | Program Analysis Precision & Verified Remediation Foundations |
| [v1.7](releases/RELEASE-NOTES-v1.7.md) | Verified Security, Architecture Contracts & Reliability Engineering |
| [v1.6](releases/RELEASE-NOTES-v1.6.md) | Advanced Program Analysis, Security Verification & Reliability Engineering |
| [v1.5](releases/RELEASE-NOTES-v1.5.md) | Interprocedural Analysis & Verified Reference Implementation |
| [v1.4](releases/RELEASE-NOTES-v1.4.md) | Database, Security & Architecture Auditor |
| [v1.3](releases/RELEASE-NOTES-v1.3.md) | Architectural Auditor |
| [v1.2](releases/RELEASE-NOTES-v1.2.md) | Deeper engineering decisions |
| [v1.1 merge notes](releases/MERGE-NOTES.md) | how the v1.1 reference pack was merged into the original plugin |

## Ground-truth corpora and fixtures

| Document | Purpose |
|---|---|
| [`tests/fixtures/truth16/README.md`](../tests/fixtures/truth16/README.md) | the first ground-truth corpus (v1.6) |
| `tests/fixtures/truth17/*/EXPECTATIONS.md`, `tests/fixtures/truth18/*/EXPECTATIONS.md` | expectations written before the detectors, with their change logs |
| [`tests/fixtures/remediation19/README.md`](../tests/fixtures/remediation19/README.md) | the fixture that reproduces the three remediation recipes |
| `tests/fixtures/design20/specs/` | architecture contracts that must validate (`valid/`) and must be rejected with the listed JSON paths (`invalid/`, `x-expect`) |
| `tests/fixtures/regressions_v20/nested_routers/`, `route_composition/` | router dependencies propagated through `include_router` (negative control) and route paths composed through nested, multiply-mounted and dynamically-prefixed routers |
| `tests/fixtures/regressions_v20/named_authz/`, `named_authz_privilege/` | dependencies named like a policy that decide nothing (seven negative controls) next to policies that show their decision (security hardening: a name is never an authorization kind) |
| [`tests/fixtures/design20/guarantees/`](../tests/fixtures/design20/guarantees/README.md) | the frozen `guarantees` block of every example and fixture contract; any change in what the engine claims is a deliberate edit here |
| [`tests/fixtures/adoption20/handmade_shop/`](../tests/fixtures/adoption20/handmade_shop/README.md) | a hand-written FastAPI application (no src layout, module-level storage, idiosyncratic formatting) used to test adoption and anchored transforms |
| [`tests/schemas/README.md`](../tests/schemas/README.md) | the vendored SARIF 2.1.0 schema |

Guides for operators (installation, auditing a repository, remediating in an isolated copy) are in the root README and in `SKILL.md`; `guides/` holds the ones that outgrew them.
