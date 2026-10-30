# fastapi-architect

A Claude Code skill that gives FastAPI projects the **right amount** of architecture: it chooses between a prototype, a modular monolith, ports and adapters, or microservices based on the project's actual requirements, enforces module boundaries mechanically, and applies production code standards.

> 🇲🇽 **Español** más abajo.

## Why another FastAPI skill

Most FastAPI skills either teach framework syntax or impose one architecture (usually full Clean Architecture with a DI container) on every project. Both fail in practice: a CRUD API wrapped in five layers is as costly as a 40-endpoint `main.py`.

This skill instead:

- **Right-sizes the architecture.** Four stages, with explicit signals for moving between them. The default for production work is a modular monolith; ports and adapters are applied per module, only where the domain needs them.
- **Knows when a monolith should become microservices, and when it should not.** It applies hard criteria (independent scaling, team ownership, release coupling, isolation, runtime needs) plus prerequisites, and pushes back when a split would produce a distributed monolith. When extraction is justified, it follows a strangler-fig playbook.
- **Enforces boundaries with tooling, not good intentions.** `import-linter` contracts make cross-module imports and layer violations fail CI.
- **Ships verified code.** Every snippet in the references passes `ruff`, `mypy --strict` and `import-linter`, and the example module's tests pass against PostgreSQL with current FastAPI, Starlette, SQLAlchemy 2.x and Pydantic v2, with warnings treated as errors.
- **Uses progressive disclosure.** `SKILL.md` holds the decision workflow; detailed references load only when the task needs them.

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add martinDTV/claude-fastapi-architect-skill
/plugin install fastapi-architect@fastapi-architect
```

### Manually

Copy the skill folder into your user or project skills directory:

```bash
git clone https://github.com/martinDTV/claude-fastapi-architect-skill.git

# For every project (user scope)
cp -r claude-fastapi-architect-skill/plugins/fastapi-architect/skills/fastapi-architect ~/.claude/skills/

# Or only for one project (commit it so your team gets it too)
cp -r claude-fastapi-architect-skill/plugins/fastapi-architect/skills/fastapi-architect .claude/skills/
```

## Usage

The skill activates automatically when you work on a FastAPI project. Example prompts:

- "Create a FastAPI backend for a clinic appointment system with patients, doctors and appointments."
- "Add an invoicing module to this API."
- "Our FastAPI app is getting big. Should we move to microservices?"
- "Review this FastAPI project's structure and tell me what to fix first."
- "Extract the notifications module into its own service."
- "Audit this FastAPI repository and tell me what is actually broken versus what is just style."

## What's inside

```
.claude-plugin/marketplace.json           # marketplace catalog
plugins/fastapi-architect/
├── .claude-plugin/plugin.json            # plugin manifest
└── skills/fastapi-architect/
    ├── SKILL.md                          # decision workflow and non-negotiable rules
    ├── references/
    │   ├── project-structure.md          # layouts per stage, full example module
    │   ├── architecture-decisions.md     # stage guide, import-linter, microservice readiness, extraction playbook
    │   ├── code-standards.md             # typing, dependencies, async, schemas, errors (RFC 9457)
    │   ├── data-layer.md                 # SQLAlchemy 2.x, transactions, repositories, Alembic, multi-tenancy
    │   ├── testing.md                    # fixtures, per-test rollback, unit and integration tests
    │   ├── security-and-config.md        # settings, secrets, OIDC/JWT, authorization, CORS
    │   └── operations.md                 # logging, request IDs, tracing, health checks, Docker, CI
    ├── assets/
    │   ├── pyproject.toml                # uv, ruff, mypy, pytest, import-linter configured
    │   ├── Dockerfile                    # multi-stage, uv, non-root
    │   ├── dockerignore.txt
    │   ├── pre-commit-config.yaml
    │   └── adr-template.md
    └── scripts/
        ├── audit_fastapi.py              # read-only static auditor entry point (stdlib only)
        ├── audit_diff.py                 # report comparison tool (v1.8)
        └── fastapi_audit/                # auditor package: rules per category, facts, summaries, call graph, report, remediation, cli
plugins/fastapi-architect/evals/          # claude plugin eval suite (positive, negative, decision, tool-use cases)
plugins/fastapi-architect/examples/       # tenant-ledger: verified reference implementation (FastAPI, SQLAlchemy 2, Alembic, PostgreSQL RLS)
evals/evals.json                          # human-scorer prompts for evaluating the skill
scripts/audit_fastapi.py                  # root entry point forwarding to the skill's auditor
scripts/validate_structure.py             # packaging, links and version checks
scripts/audit_precision.py                # precision/recall/coverage/time/memory of the auditor, current vs. a previous commit
scripts/audit_diff.py                     # compare two audit reports by fingerprint: new, no longer detected, persistent, confidence/severity changes (v1.8)
tests/                                    # auditor suites per category, fixtures (incl. the truth16/17/18 ground-truth corpora), PostgreSQL integration tests
```

## Principles

1. Optimize for change, not for imagined scale.
2. Boundaries before distribution.
3. Every layer must pay rent.
4. Reversible decisions fast; irreversible ones get an ADR.

## Contributing

Issues and pull requests are welcome. Any code added to `references/` must pass `ruff format --check`, `ruff check`, `mypy --strict` and, where applicable, tests against PostgreSQL. Run `python3 scripts/validate_structure.py` and `python3 -m unittest discover -s tests -v` before any PR, and `claude plugin validate .` before opening a PR that touches the manifests.

## License

[MIT](LICENSE)

---

## 🇲🇽 Español

Skill para Claude Code que le da a los proyectos FastAPI **la cantidad correcta** de arquitectura. Elige entre prototipo, monolito modular, puertos y adaptadores o microservicios según los requisitos reales del proyecto. Además, hace cumplir los límites entre módulos con herramientas y aplica estándares de código de producción.

**Qué la hace diferente:**

- No impone Clean Architecture a todo. El punto de partida para producción es un monolito modular, y las capas extra se agregan solo a los módulos que las necesitan.
- Sabe cuándo un monolito debe pasar a microservicios y cuándo no. Aplica criterios concretos, verifica prerrequisitos y se opone cuando dividir crearía un "monolito distribuido".
- Los límites entre módulos se validan con `import-linter` en CI, no con buena voluntad.
- Todo el código de las referencias está verificado con `ruff`, `mypy --strict`, `import-linter` y tests contra PostgreSQL.

**Instalación:**

```
/plugin marketplace add martinDTV/claude-fastapi-architect-skill
/plugin install fastapi-architect@fastapi-architect
```

O copia manualmente la carpeta `plugins/fastapi-architect/skills/fastapi-architect` a `~/.claude/skills/` (todos tus proyectos) o a `.claude/skills/` (un solo proyecto).


## Extended guidance (v1.1)

This distribution includes **all original files** plus eight optional decision references in `plugins/fastapi-architect/skills/fastapi-architect/references/` and ten additional evaluation scenarios in `evals/scenarios-v2.json`. The main `SKILL.md` routes to these references only when the task needs them.

The added references cover architecture decisions, API contracts, tenancy/identity, async concurrency, distributed reliability, database evolution, adversarial testing and production operations. This is an additive expansion rather than a replacement for the original seven references.

**Updating an existing checkout:** extract the archive contents into the repository root. Do not replace your `.git` directory (the archive intentionally excludes it). Review `git status` and `git diff`, run your validation, and commit only the intended changes. No cleanup or deletion is required.

## Extension v1.2 — deeper engineering decisions

Adds six opt-in reference guides for domain modeling, FastAPI/ASGI runtime, identity threat modeling, durable workflows, performance/capacity, and upgrade governance. All seven original guides and the eight v2 guides remain intact.

This is a complete repository overlay: extract at the **root** of an existing clone. It does not contain `.git`, request deletion of tracked files, or replace the installation identity `fastapi-architect`. Review changes with `git diff --check` and validate in Claude Code with `claude plugin validate .` before publishing.

Validation performed on the package: manifest JSON parsed, expected reference files present, references linked from `SKILL.md`, and ZIP integrity checked. These checks do not prove Claude-generated code correctness or a passing Claude evaluation run.

## Extension v1.3 — Architectural Auditor

Adds a **read-only static auditor** and two opt-in references for auditing existing FastAPI repositories and for building verified reference implementations. Everything from v1.0, v1.1 and v1.2 remains intact; the plugin identity and install commands do not change.

```bash
python3 scripts/audit_fastapi.py /path/to/project            # text summary
python3 scripts/audit_fastapi.py /path/to/project --json     # deterministic JSON report
python3 scripts/audit_fastapi.py --list-rules
```

- Standard library only (`ast`); it never imports, executes or modifies the inspected code. Skips `.git`, virtualenvs, caches, `node_modules`, `dist`, `build` and dot-directories; does not follow symlinked directories; analyses each real file once.
- Rules: `ASYNC_BLOCKING_CALL` (blocking calls such as `time.sleep`, `requests.*`, `urllib.request.urlopen`, `subprocess.run` inside `async def`, with import aliases and local shadowing handled), `ASYNCIO_RUN_NESTED`, `ROUTE_NO_RETURN_ANNOTATION` (informational; suppressed by `response_model`/`response_class`), `PY_SYNTAX`, `FILE_SKIPPED`.
- Every finding is a heuristic signal with a severity and a confidence, not a demonstrated defect. `references/architecture-audit.md` defines how to verify, classify (four evidence states), reproduce, refactor incrementally and report. `references/reference-implementation-strategy.md` defines how runnable examples must be built, labelled and verified; no validated end-to-end example ships yet.
- Test code is classified, never hidden: every finding carries `environment` (`application` or `test`), blocking calls in test code drop one confidence level, `--exclude-tests` leaves tests out explicitly, and documented suppressions (`# noqa: ASYNC2xx`, `# audit-fastapi: ignore[RULE]`) are kept in the report as `suppressed` and ignored by `--fail-on`. Report schema 1.1 is a superset of 1.0.
- Evals: `plugins/fastapi-architect/evals/` holds the native `claude plugin eval` suite with positive, negative, architecture-decision and tool-use cases plus activation criteria; see its README for commands, costs and the recorded baseline.
- Tests: `python3 -m unittest discover -s tests -v` (also runs under pytest). Release details in `RELEASE-NOTES-v1.3.md`.

## Extension v1.4 — Database, Security & Architecture Auditor

The auditor becomes a small standard-library package (`fastapi_audit`) with rules for SQLAlchemy 2.x sessions and engines, transactions and concurrency, Alembic migrations, authorization and multi-tenancy signals, and architecture boundaries. Everything from v1.0 to v1.3 remains; the command, options and report fields of v1.3 are unchanged (report schema 1.2 is additive).

```bash
python3 scripts/audit_fastapi.py /path/to/project --format markdown          # report for a review
python3 scripts/audit_fastapi.py /path/to/project --category sqlalchemy,transactions,migrations
python3 scripts/audit_fastapi.py /path/to/project --baseline previous.json --fail-on-new
python3 scripts/audit_fastapi.py /path/to/project --sarif audit.sarif                 # SARIF 2.1.0 for code scanning (v1.7)
python3 scripts/audit_fastapi.py /path/to/project --changed-files <(git diff --name-only main)
```

- 35 rules in 8 categories. Each finding carries a `verification` field (`static`, `needs_runtime_check`, `not_verifiable_statically`) and a recommended test; `references/audit-rules.md` documents what every rule proves, its false-positive patterns and the test that closes it.
- Session types come from annotations and module-level factories, so a correctly used global `sessionmaker` is never reported while a shared `Session` instance is. Architecture layers and business modules are inferred from paths or configured in `.audit-fastapi.json`; existing import-linter contracts are detected and treated as authoritative.
- Migrations are never executed. Destructive operations are reported as changes that need review with their expand-contract context.
- Tests: 149 across unit suites per category, deliberately correct and incorrect fixture projects, and PostgreSQL integration tests that reproduce the lost update, the session-level `SET` leak and the `NOT NULL` failure on an ephemeral cluster (`tests/integration`, skipped when PostgreSQL tools are absent). Release details in `RELEASE-NOTES-v1.4.md`.

## Extension v1.5 — Interprocedural Analysis & Verified Reference Implementation

The auditor gains a conservative interprocedural engine and the plugin ships its first verified reference application. Everything from v1.0 to v1.4 remains; every v1.4 command, option and report field is unchanged (report schema 1.3 is additive).

- **Call graph.** Per-function summaries and syntactic resolution (imports, module-level names, `self.method`, instances of project classes, declared bases, `src.`/package aliases). Session kinds now flow from `Depends(f)` return types and from callers into untyped parameters; each finding states its `evidence` (`verified`, `inferred`, `unknown`) and cross-function findings carry a `call_path`. Limits are explicit and reported: `--max-call-depth` (default 4), a time budget, bounded propagation; `--no-interprocedural` restores v1.4 behaviour.
- **Six rules on top of it.** Blocking reached through synchronous helpers, nested `session.begin()`, implicit lazy loads under `AsyncSession`, retried writes without an idempotency signal, request-scoped sessions handed to background tasks, and background-task context classification (captured / recreated / unverifiable). 41 rules in 9 categories; `references/audit-engine.md` documents the engine, `references/audit-rules.md` the rules.
- **Reference implementation.** `plugins/fastapi-architect/examples/tenant-ledger`: multi-tenant ledger with PostgreSQL row-level security on a non-owner role, atomic and optimistic concurrency, idempotency keys, Alembic migrations, RFC 9457 errors and demo authentication clearly marked as not for production. Its 22 tests run against a throwaway PostgreSQL cluster and prove authorized/denied operations, cross-tenant denial through the API and below it, 50 concurrent deposits without a lost update, rollback, migrations from empty and from the previous revision, and two anti-patterns reproduced next to their fixes. `VERIFICATION.md` records the run.
- Tests: 173 in the plugin plus 22 in the example. Evals: 31 cases (12 new), validated offline. Release details in `RELEASE-NOTES-v1.5.md`.

## Extension v1.6 — Advanced Program Analysis, Security Verification & Reliability Engineering

The interprocedural engine resolves far more of a real codebase without guessing, every finding keeps its evidence, and the report states how much it could and could not see. Everything from v1.0 to v1.5 remains; every v1.5 command, option, rule id and report field is unchanged (report schema 1.4 is additive).

- **Symbol resolution.** Relative imports, facade re-exports (`app/__init__.py`, `public.py`), instance attributes typed in `__init__` (`self._session`, `self._repo`), annotated return types, functions handed as arguments (resolved only when every caller agrees), static and class methods, module-level instances, and Protocols/abstract classes with exactly one implementation (edges marked `inferred`). Sites with several candidates stay `ambiguous` and never produce a finding; builtins, library receivers and module-level objects are classified instead of being counted as unknown. On two real repositories the unresolved share fell from 41.6% to 12.8% and from 58.6% to 18.5%, with no v1.5 finding lost; every new finding was reviewed by hand and the false-positive shapes became tightenings with regression tests.
- **Honest coverage.** The `interprocedural` block reports `coverage` (project-call coverage and classified ratio), `uncertainty` (ambiguous sites, dynamic sites, unresolved callback parameters, interfaces), `edges` by kind and `limits`; `--time-budget` bounds the pass and truncation is reported, never silent. Pathological files produce `ANALYSIS_PARTIAL` instead of a crash.
- **Rules.** Six evidence-based additions: dropped `create_task` references, unbounded retries, HTTP calls without timeout, dual writes without an outbox, tenant id not bound to the authenticated principal (a question, `evidence: unknown`), privilege changes with only a generic authentication signal (a question). Existing session, transaction, blocking and security rules now reach instance attributes, call paths and composed dependencies; commit-without-rollback and dual-write detection were tightened against real false positives. 48 rules in 9 categories.
- **Precision measured, not asserted.** `tests/fixtures/truth16` is a ground-truth corpus with inline markers written before the detectors ran; `scripts/audit_precision.py` scores precision, recall, F1, expected edges, coverage, time and peak memory for the current engine and for any previous commit. v1.5 → v1.6 on the corpus: F1 0.118 → 1.0, expected edges 2/7 → 7/7, no regression on the eleven existing fixtures.
- **Reference implementation.** `tenant-ledger` gains five adversarial tests on real PostgreSQL (27 in total): a shared session silently mixing two transactions, a request-scoped session that loses its tenant context in a background task, cancellation by timeout, a lost response retried with and without an idempotency key, and the RLS configuration invariants. `VERIFICATION.md` is renewed; the label stays `verified`.
- Tests: 210 in the plugin plus 27 in the example. Evals: 39 cases (8 new, including an adversarial defence of two false "false positives" and a Django/Celery negative), validated offline; no paid runs. Release details in `RELEASE-NOTES-v1.6.md`.

## Extension v1.7 — Verified Security, Architecture Contracts & Reliability Engineering

The auditor now states, per route, what it can see about authentication and authorization, asks fewer and sharper questions, and is measured against a corpus whose expectations were written before the detectors. Everything from v1.0 to v1.6 remains; every option, rule id and report field is unchanged (report schema 1.5 is additive).

- **Security contracts.** The `contracts` block lists every route with its authentication (dependency chains, permission factories, class-based checkers, `Security` scopes, router and application dependencies, decorators), its authorization kinds (role, scopes, decorator, membership, resource dependency, principal flowing with the resource, authorization-service call, tenant comparison), its principal, tenant and resource parameters, and whether it is public by declaration (`public_routes`, `# audit-fastapi: public`, health-style paths). Domain tenant names are declared with `tenant_params`. On two real repositories: 167 routes, all authenticated, 127 with a visible authorization kind, 2 public; 19 routes, 5 with authorization unknown.
- **Questions, not verdicts.** `SEC_RESOURCE_AUTHZ_UNVERIFIED` (resource id handed to project code without the principal, nothing visible relating them) and `SEC_RLS_CONTEXT_FROM_REQUEST` (RLS context set from the tenant the caller named, in a route that does not bind it to the principal; it replaces the generic tenant question) join the existing ones; `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` respects declared-public routes and no longer treats collection endpoints as public.
- **Lifecycle and consumers.** `ASYNC_COROUTINE_NOT_AWAITED` (project coroutine dropped, resolved through the call graph), `ASYNC_LOCK_PER_CALL`, `REL_HTTP_CLIENT_PER_CALL`, and `REL_CONSUMER_WITHOUT_INBOX` as a question for handlers that write without a redelivery guard; a task created with the session and awaited right away is no longer reported as sharing it.
- **Evidence states and SARIF.** Every finding carries one `evidence_state` (`static_confirmed`, `inferred`, `insufficient`, `partial`, `suppressed`; `reproduced` is reserved for runtime records and never produced statically) and the summary counts them. `--format sarif` / `--sarif FILE` export SARIF 2.1.0 with stable fingerprints and in-source suppressions; text, JSON and markdown are unchanged.
- **Declared contracts (opt-in, fail closed).** `contracts` in `.audit-fastapi.json`: authenticated prefixes, mutations that must show an authorization kind, forbidden imports between module prefixes, and the layers that own commits. Four `CONTRACT_*` rules report only disagreements with those declarations; an invalid declaration stops the audit with every problem and its JSON path (exit 2, no report); the configuration is plain JSON and no project Python is ever executed. A health-check naming convention never declares a route public: such routes are `exposure_undetermined` until declared, and a declared contract applies to them. SARIF documents validate against the official OASIS 2.1.0 schema. 58 rules in 10 categories.
- **Independent corpus.** `tests/fixtures/truth17` has a `dev` partition used during implementation and an `acceptance` partition held out and scored once at release time; `scripts/audit_precision.py` reports precision, recall, F1, false-positive and false-negative rates, unknown-evidence rate and the verified/inferred/unknown split. Three partitions: `dev` (used during implementation, F1 0.684 → 1.0), `acceptance` (held out, scored at the first release candidate: F1 0.636 → 0.783 as first measured, 0.815 after one disclosed engine fix, 0.963 once the project declares its tenant parameter) and `holdout` (written before the last features, scored once with the final engine: F1 0.519 → 0.895). None of the held-out partitions was edited; their open items are analysed in the release notes.
- **Reference implementation.** `tenant-ledger` (29 tests) reproduces on PostgreSQL a tenant check that is present but not bound: with the RLS context set from the URL tenant a member of ACME reads Globex's accounts through correctly configured policies; membership first returns 404.
- Tests: 251 in the plugin plus 29 in the example. Evals: 47 cases (8 new), validated offline; no paid runs. Release details in `RELEASE-NOTES-v1.7.md`.

## Extension v1.8 — Program Analysis Precision & Verified Remediation Foundations

The auditor classifies more call sites without inventing project edges, reads the structure of a loop before calling a commit accidental, attaches a structured remediation proposal it never applies, and compares two reports without ever saying "resolved". Everything from v1.0 to v1.7 remains; every option, rule id, fingerprint and report field is unchanged (report schema 1.6 is additive over 1.5).

- **Call classification.** Locals bound to library or builtin results (plain, awaited, unpacked, `with`), project functions returning builtin or library types, closures reading the enclosing function's typed names, ASGI `send`/`receive`/`app` callbacks, typed `Callable` parameters (v1.7 missed the generic form), lambdas that only forward to one target, bound-method references, `self.a.b.method()` through two typed links, same-module class annotations, mixins whose concrete subclasses agree on an attribute's kind, template-method hooks with one implementation, and unknown methods of classes with a third-party base. Loop targets and comprehension variables stay dynamic and say why: every dynamic site carries an origin (`uncertainty.dynamic_by_origin`), coverage is reported as three shares that sum to one, and `callback_params_unresolved` now counts parameters, not sites, and excludes framework callbacks. On the two calibration repositories dynamic sites fell by 18 % and 12 %, `not_found` by 37 % and 96 %, and every previously resolved edge kept its target.
- **Transactional precision.** `TX_COMMIT_IN_LOOP` classifies the loop by structure (`per_item`, `batch`, `paginated`, `convergence`, `retry`): per-item commits are reported as before with `details.loop_kind` and the evidence; structurally deliberate kinds are counted in `facts.commit_loops` and not reported (v1.7 silenced only the modulo form; names such as `BATCH_SIZE` prove nothing). New `TX_COMMIT_INSIDE_BEGIN_BLOCK` (high): `commit()` inside `with session.begin()`. 59 rules.
- **Remediation foundations.** Five rules carry a `remediation` object: problem, evidence, certainty (from the evidence state), proposed fix as steps, invariants, tests, regression risk. `status: proposed`, `applied: false`, `verified: false`, always; the auditor never edits code.
- **Report comparison.** `scripts/audit_diff.py BEFORE.json AFTER.json` and the richer `--baseline` block: new, no longer detected, persistent, severity/confidence/evidence-state changes, suppression transitions, partial-analysis reliability. A finding that is not reported any more is "no longer detected", never "resolved".
- **Measured.** `tests/fixtures/truth18` (`dev` scored in-process, `acceptance` scored once at release time): on `dev` the v1.7 engine scored F1 0.308, expected edges 1/6 and unresolved statuses 7/16; v1.8 scores 1.0, 6/6 and 16/16. Clivana (read-only) 55 → 52 findings: the three that disappeared are a convergence loop, a cursor-paginated job and a bounded batch deletion; AgentForge 11 → 16: four indirect-blocking findings in development scripts (async code calling a worktree manager that shells out to git, followed through a two-level attribute chain) and one resource question on a route whose store call v1.7 could not follow. Time +6 % on the largest repository, memory flat. The reference implementation gains a PostgreSQL test for the new rule (30 tests); it corrected the rule's first draft. Release details in `RELEASE-NOTES-v1.8.md`.
