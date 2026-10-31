# Documentation index

The repository root holds the entry point (`README.md`); everything else lives here or inside the plugin, where Claude Code expects it.

## Contractual documents (do not move)

| Document | Purpose |
|---|---|
| [`plugins/fastapi-architect/skills/fastapi-architect/SKILL.md`](../plugins/fastapi-architect/skills/fastapi-architect/SKILL.md) | the skill: workflow, stages, standards, auditor and remediation usage |
| [`plugins/fastapi-architect/skills/fastapi-architect/references/`](../plugins/fastapi-architect/skills/fastapi-architect/references/) | the 27 reference documents the skill routes to (architecture, data layer, security, auditor engine and rules, safe refactoring, design contracts, …) |
| [`plugins/fastapi-architect/evals/README.md`](../plugins/fastapi-architect/evals/README.md) | the offline evaluation suite (`claude plugin eval` format) |
| [`plugins/fastapi-architect/examples/tenant-ledger/`](../plugins/fastapi-architect/examples/tenant-ledger/README.md) | the verified reference implementation (README, VERIFICATION, LIMITATIONS) |
| [`plugins/fastapi-architect/examples/specs/`](../plugins/fastapi-architect/examples/specs/) and [`examples/generated/`](../plugins/fastapi-architect/examples/generated/README.md) | the two example architecture contracts (v2.0) and the backends generated from them (`inventory-service`, `orders-platform`), with the [verification evidence](../plugins/fastapi-architect/examples/generated/VERIFICATION.md) of the PostgreSQL run |

## Architecture records

| Document | Purpose |
|---|---|
| [`architecture/design-engine-v2.md`](architecture/design-engine-v2.md) | why the v2.0 design engine is a declarative contract, a conservative advisor, a pure blueprint and a guarded writer, and how it reuses the audit and remediation engines |

## Guides

| Document | Purpose |
|---|---|
| [`guides/generated-postgresql-projects.md`](guides/generated-postgresql-projects.md) | persistence, Alembic migrations, the generated test suite and the opt-in verification of a generated PostgreSQL backend against a throwaway cluster |

## Release notes

| Version | Title |
|---|---|
| [v2.0](releases/RELEASE-NOTES-v2.0.md) | Architecture & Engineering Foundations (first increment: design engine) |
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
| [`tests/schemas/README.md`](../tests/schemas/README.md) | the vendored SARIF 2.1.0 schema |

Guides for operators (installation, auditing a repository, remediating in an isolated copy) are in the root README and in `SKILL.md`; `guides/` holds the ones that outgrew them.
