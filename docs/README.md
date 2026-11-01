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
| [`architecture/discovery-v2.1.md`](architecture/discovery-v2.1.md) | the v2.1.0 discovery: state of the five areas (phased migrations, evolution of existing code, isolated execution, compatibility, technical debt), ADRs, contracts that evolve, the increment plan, risks, the compatibility matrix, the acceptance criteria and the decisions taken on review |
| [`architecture/discovery-v2.2.md`](architecture/discovery-v2.2.md) | the v2.2.0 discovery: safe renames (expand, retire, contract), dual write and compatible read, span-exact transforms, three-way merge, adopted projects, recovery and concurrency; ADRs, contracts, guarantees, risks, compatibility matrix, roadmap, acceptance criteria and the decisions taken on review |
| [`architecture/adr-2.2-01-safe-renames.md`](architecture/adr-2.2-01-safe-renames.md) | why a column rename is a phased change with a mirror backfill and a scoped sync trigger that refuses contradictions, which version reads and writes which column in each of the three evolutions (expand, retire, destructive contract), why the drop needs sixteen named preconditions, an irreversible confirmation and identified sessions, and what its downgrade can and cannot restore
| [`architecture/adr-2.2-05-three-way-merge.md`](architecture/adr-2.2-05-three-way-merge.md) | why the merge of a hand-modified generated file is opt-in per exact file with an ownership boundary, how BASE is authenticated by the manifest's digest, what merges and what is a structured conflict, why apply never recomputes a merge and what `verified` means for it |
| [`architecture/adr-2.2-06-adopted-projects-ii.md`](architecture/adr-2.2-06-adopted-projects-ii.md) | why a hand-written project receives new modules only at one mount point the operator declares and the tool verifies, how a generated module stays self-contained inside an adopted project, what an extension of an adopted router may declare (typed parameters, allowed dependencies, one delegate, a response model) and what it may not, when persistence is possible (the project's own, verified Alembic environment) and what is refused by name |
| [`architecture/adr-2.1-01-phased-migrations.md`](architecture/adr-2.1-01-phased-migrations.md) | why a schema change that existing rows cannot satisfy is four revisions and a program (expand, backfill, verify, contract), the declared/applied/verified distinction, declarative backfills only, online but not lock-free, the stated concurrency semantics, the contract as a human decision, verified concurrent indexes, compatibility and what stays deferred |
| [`architecture/adr-2.1-03-isolated-container-executor.md`](architecture/adr-2.1-03-isolated-container-executor.md) | why untrusted code runs only in an OCI container behind the existing executor seam (Docker first, runtime-neutral domain), what is mandatory and verified twice per run, how dependencies are prepared without the host, and what is not protected |

## Guides

| Document | Purpose |
|---|---|
| [`guides/generated-postgresql-projects.md`](guides/generated-postgresql-projects.md) | persistence, Alembic migrations, the generated test suite and the opt-in verification of a generated PostgreSQL backend against a throwaway cluster |
| [`guides/evolving-generated-projects.md`](guides/evolving-generated-projects.md) | discover an existing project, write an evolution contract, preview the plan, approve and apply it with verification in a copy; changing existing modules (1.1), adopting a hand-written project, recovering an interrupted write; what the engine protects |
| [`guides/identity-authorization-and-rls.md`](guides/identity-authorization-and-rls.md) | the five security layers of a schema 1.2 contract (OIDC or API-key grants, principal-tenant binding, per-operation roles, ownership, row-level security), what is generated for each, how to run the generated proofs with two database roles, how to read 401/403/404/503, evolving safely, limits |
| [`guides/phased-migrations-and-backfills.md`](guides/phased-migrations-and-backfills.md) | adding a NOT NULL column or a concurrent index to a PostgreSQL project with live rows: the expand contract (1.2), the generated resumable backfill runner and its guarantees, the migration evidence, the authorized contract and its preconditions, the lock profile of every phase, what is deliberately out of scope and what was verified on a real cluster |
| [`guides/isolated-execution-with-containers.md`](guides/isolated-execution-with-containers.md) | using `--executor container` on the three tools, what a run does step by step, what the report records, what it does not protect against (runtime, kernel, daemon, preparation egress, provenance), and what was verified on this project's machine |

## Release notes

| Version | Title |
|---|---|
| [v2.2 (in progress)](releases/RELEASE-NOTES-v2.2.md) | Safe Evolution II: increment 0 (span-exact edits, the pure three-way merge, the project lock, journal 1.1, the adversarial corpus and the v2.1.0 compatibility fixtures) implemented; increments 1 to 6 pending authorisation |
| [v2.1](releases/RELEASE-NOTES-v2.1.md) | Safe Change at Scale, published 2026-10-31: engineering foundations and technical debt (increment 0), the isolated container executor (increment 1), phased PostgreSQL migrations with safe backfills (increment 2) and the acceptance (increment 6) |
| [v2.0](releases/RELEASE-NOTES-v2.0.md) | Architecture & Engineering Foundations (increments 1 to 5: design engine, PostgreSQL, evolution, adoption, identity and tenant isolation) |
| [v2.1.0 acceptance report](releases/ACCEPTANCE-v2.1.0.md) | release-candidate review of the three v2.1 increments as one system: integrity, capability matrix (recommended / implemented / tested / not supported), backfills with locked rows, the semantics of `complete`, contract evidence by what it rests on, the Docker executor on a real daemon, PostgreSQL results, the two end-to-end scenarios, compatibility, tests and validations, defects, residual risks, documentation state and the verdict |
| [v2.0.0 acceptance report](releases/ACCEPTANCE-v2.0.0.md) | release-candidate audit of the whole v2.0.0 range: integrity, capability matrix, end-to-end cycle, architecture, security limits, compatibility, recovery, executed validations, defects fixed, residual risks, verdict |
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
