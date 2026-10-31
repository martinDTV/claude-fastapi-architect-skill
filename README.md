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
    │   ├── operations.md                 # logging, request IDs, tracing, health checks, Docker, CI
    │   ├── design-contracts.md           # v2.0: architecture contract, advisor rules, generation safety, report schema
    │   └── evolution-contracts.md        # v2.0: project discovery, evolution contract, plan, safe writer, verification states
    ├── assets/
    │   ├── pyproject.toml                # uv, ruff, mypy, pytest, import-linter configured
    │   ├── Dockerfile                    # multi-stage, uv, non-root
    │   ├── dockerignore.txt
    │   ├── pre-commit-config.yaml
    │   └── adr-template.md
    └── scripts/
        ├── audit_fastapi.py              # read-only static auditor entry point (stdlib only)
        ├── audit_diff.py                 # report comparison tool (v1.8)
        ├── audit_remediate.py            # verified remediation tool (v1.9)
        ├── design_fastapi.py             # design engine entry point: contract -> recommendation -> project -> verification (v2.0)
        ├── evolve_fastapi.py             # discover, propose, adopt, plan, apply, recover: evolution of generated and adopted projects (v2.0)
        ├── fastapi_audit/                # auditor package: rules per category, facts, summaries, call graph, report, remediation, remediate/, cli
        └── fastapi_design/               # design package: spec, advisor, blueprint, generator, verify, report, pipeline, discovery, evolution, journal, transforms, adoption (v2.0)
plugins/fastapi-architect/evals/          # claude plugin eval suite (positive, negative, decision, tool-use cases)
plugins/fastapi-architect/examples/       # tenant-ledger: verified reference implementation (FastAPI, SQLAlchemy 2, Alembic, PostgreSQL RLS);
                                          # specs/ and evolutions/: example architecture and evolution contracts; generated/: the backends generated from them (v2.0)
evals/evals.json                          # human-scorer prompts for evaluating the skill
scripts/audit_fastapi.py                  # root entry point forwarding to the skill's auditor
scripts/validate_structure.py             # packaging, links and version checks
scripts/audit_precision.py                # precision/recall/coverage/time/memory of the auditor, current vs. a previous commit
scripts/audit_diff.py                     # compare two audit reports by fingerprint: new, no longer detected, persistent, confidence/severity changes (v1.8)
scripts/audit_remediate.py                # patch findings in an isolated copy, re-audit and re-test; never writes to the project (v1.9)
scripts/design_fastapi.py                 # validate an architecture contract, recommend a structure, generate the project, audit it and run its tests under the trust model (v2.0)
scripts/evolve_fastapi.py                 # discover / propose / adopt / plan / apply / recover: evolution verified in a copy, written through the recoverable journal (v2.0)
tests/                                    # auditor suites per category, fixtures (incl. the truth16/17/18 ground-truth corpora), PostgreSQL integration tests
docs/                                     # documentation index (docs/README.md), release notes (docs/releases/), architecture records (docs/architecture/), guides (docs/guides/)
```

Full documentation index: [`docs/README.md`](docs/README.md).

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
- Tests: `python3 -m unittest discover -s tests -v` (also runs under pytest). Release details in [`docs/releases/RELEASE-NOTES-v1.3.md`](docs/releases/RELEASE-NOTES-v1.3.md).

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
- Tests: 149 across unit suites per category, deliberately correct and incorrect fixture projects, and PostgreSQL integration tests that reproduce the lost update, the session-level `SET` leak and the `NOT NULL` failure on an ephemeral cluster (`tests/integration`, skipped when PostgreSQL tools are absent). Release details in [`docs/releases/RELEASE-NOTES-v1.4.md`](docs/releases/RELEASE-NOTES-v1.4.md).

## Extension v1.5 — Interprocedural Analysis & Verified Reference Implementation

The auditor gains a conservative interprocedural engine and the plugin ships its first verified reference application. Everything from v1.0 to v1.4 remains; every v1.4 command, option and report field is unchanged (report schema 1.3 is additive).

- **Call graph.** Per-function summaries and syntactic resolution (imports, module-level names, `self.method`, instances of project classes, declared bases, `src.`/package aliases). Session kinds now flow from `Depends(f)` return types and from callers into untyped parameters; each finding states its `evidence` (`verified`, `inferred`, `unknown`) and cross-function findings carry a `call_path`. Limits are explicit and reported: `--max-call-depth` (default 4), a time budget, bounded propagation; `--no-interprocedural` restores v1.4 behaviour.
- **Six rules on top of it.** Blocking reached through synchronous helpers, nested `session.begin()`, implicit lazy loads under `AsyncSession`, retried writes without an idempotency signal, request-scoped sessions handed to background tasks, and background-task context classification (captured / recreated / unverifiable). 41 rules in 9 categories; `references/audit-engine.md` documents the engine, `references/audit-rules.md` the rules.
- **Reference implementation.** `plugins/fastapi-architect/examples/tenant-ledger`: multi-tenant ledger with PostgreSQL row-level security on a non-owner role, atomic and optimistic concurrency, idempotency keys, Alembic migrations, RFC 9457 errors and demo authentication clearly marked as not for production. Its 22 tests run against a throwaway PostgreSQL cluster and prove authorized/denied operations, cross-tenant denial through the API and below it, 50 concurrent deposits without a lost update, rollback, migrations from empty and from the previous revision, and two anti-patterns reproduced next to their fixes. `VERIFICATION.md` records the run.
- Tests: 173 in the plugin plus 22 in the example. Evals: 31 cases (12 new), validated offline. Release details in [`docs/releases/RELEASE-NOTES-v1.5.md`](docs/releases/RELEASE-NOTES-v1.5.md).

## Extension v1.6 — Advanced Program Analysis, Security Verification & Reliability Engineering

The interprocedural engine resolves far more of a real codebase without guessing, every finding keeps its evidence, and the report states how much it could and could not see. Everything from v1.0 to v1.5 remains; every v1.5 command, option, rule id and report field is unchanged (report schema 1.4 is additive).

- **Symbol resolution.** Relative imports, facade re-exports (`app/__init__.py`, `public.py`), instance attributes typed in `__init__` (`self._session`, `self._repo`), annotated return types, functions handed as arguments (resolved only when every caller agrees), static and class methods, module-level instances, and Protocols/abstract classes with exactly one implementation (edges marked `inferred`). Sites with several candidates stay `ambiguous` and never produce a finding; builtins, library receivers and module-level objects are classified instead of being counted as unknown. On two real repositories the unresolved share fell from 41.6% to 12.8% and from 58.6% to 18.5%, with no v1.5 finding lost; every new finding was reviewed by hand and the false-positive shapes became tightenings with regression tests.
- **Honest coverage.** The `interprocedural` block reports `coverage` (project-call coverage and classified ratio), `uncertainty` (ambiguous sites, dynamic sites, unresolved callback parameters, interfaces), `edges` by kind and `limits`; `--time-budget` bounds the pass and truncation is reported, never silent. Pathological files produce `ANALYSIS_PARTIAL` instead of a crash.
- **Rules.** Six evidence-based additions: dropped `create_task` references, unbounded retries, HTTP calls without timeout, dual writes without an outbox, tenant id not bound to the authenticated principal (a question, `evidence: unknown`), privilege changes with only a generic authentication signal (a question). Existing session, transaction, blocking and security rules now reach instance attributes, call paths and composed dependencies; commit-without-rollback and dual-write detection were tightened against real false positives. 48 rules in 9 categories.
- **Precision measured, not asserted.** `tests/fixtures/truth16` is a ground-truth corpus with inline markers written before the detectors ran; `scripts/audit_precision.py` scores precision, recall, F1, expected edges, coverage, time and peak memory for the current engine and for any previous commit. v1.5 → v1.6 on the corpus: F1 0.118 → 1.0, expected edges 2/7 → 7/7, no regression on the eleven existing fixtures.
- **Reference implementation.** `tenant-ledger` gains five adversarial tests on real PostgreSQL (27 in total): a shared session silently mixing two transactions, a request-scoped session that loses its tenant context in a background task, cancellation by timeout, a lost response retried with and without an idempotency key, and the RLS configuration invariants. `VERIFICATION.md` is renewed; the label stays `verified`.
- Tests: 210 in the plugin plus 27 in the example. Evals: 39 cases (8 new, including an adversarial defence of two false "false positives" and a Django/Celery negative), validated offline; no paid runs. Release details in [`docs/releases/RELEASE-NOTES-v1.6.md`](docs/releases/RELEASE-NOTES-v1.6.md).

## Extension v1.7 — Verified Security, Architecture Contracts & Reliability Engineering

The auditor now states, per route, what it can see about authentication and authorization, asks fewer and sharper questions, and is measured against a corpus whose expectations were written before the detectors. Everything from v1.0 to v1.6 remains; every option, rule id and report field is unchanged (report schema 1.5 is additive).

- **Security contracts.** The `contracts` block lists every route with its authentication (dependency chains, permission factories, class-based checkers, `Security` scopes, router and application dependencies, decorators), its authorization kinds (role, scopes, decorator, membership, resource dependency, principal flowing with the resource, authorization-service call, tenant comparison), its principal, tenant and resource parameters, and whether it is public by declaration (`public_routes`, `# audit-fastapi: public`, health-style paths). Domain tenant names are declared with `tenant_params`. On two real repositories: 167 routes, all authenticated, 127 with a visible authorization kind, 2 public; 19 routes, 5 with authorization unknown.
- **Questions, not verdicts.** `SEC_RESOURCE_AUTHZ_UNVERIFIED` (resource id handed to project code without the principal, nothing visible relating them) and `SEC_RLS_CONTEXT_FROM_REQUEST` (RLS context set from the tenant the caller named, in a route that does not bind it to the principal; it replaces the generic tenant question) join the existing ones; `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` respects declared-public routes and no longer treats collection endpoints as public.
- **Lifecycle and consumers.** `ASYNC_COROUTINE_NOT_AWAITED` (project coroutine dropped, resolved through the call graph), `ASYNC_LOCK_PER_CALL`, `REL_HTTP_CLIENT_PER_CALL`, and `REL_CONSUMER_WITHOUT_INBOX` as a question for handlers that write without a redelivery guard; a task created with the session and awaited right away is no longer reported as sharing it.
- **Evidence states and SARIF.** Every finding carries one `evidence_state` (`static_confirmed`, `inferred`, `insufficient`, `partial`, `suppressed`; `reproduced` is reserved for runtime records and never produced statically) and the summary counts them. `--format sarif` / `--sarif FILE` export SARIF 2.1.0 with stable fingerprints and in-source suppressions; text, JSON and markdown are unchanged.
- **Declared contracts (opt-in, fail closed).** `contracts` in `.audit-fastapi.json`: authenticated prefixes, mutations that must show an authorization kind, forbidden imports between module prefixes, and the layers that own commits. Four `CONTRACT_*` rules report only disagreements with those declarations; an invalid declaration stops the audit with every problem and its JSON path (exit 2, no report); the configuration is plain JSON and no project Python is ever executed. A health-check naming convention never declares a route public: such routes are `exposure_undetermined` until declared, and a declared contract applies to them. SARIF documents validate against the official OASIS 2.1.0 schema. 58 rules in 10 categories.
- **Independent corpus.** `tests/fixtures/truth17` has a `dev` partition used during implementation and an `acceptance` partition held out and scored once at release time; `scripts/audit_precision.py` reports precision, recall, F1, false-positive and false-negative rates, unknown-evidence rate and the verified/inferred/unknown split. Three partitions: `dev` (used during implementation, F1 0.684 → 1.0), `acceptance` (held out, scored at the first release candidate: F1 0.636 → 0.783 as first measured, 0.815 after one disclosed engine fix, 0.963 once the project declares its tenant parameter) and `holdout` (written before the last features, scored once with the final engine: F1 0.519 → 0.895). None of the held-out partitions was edited; their open items are analysed in the release notes.
- **Reference implementation.** `tenant-ledger` (29 tests) reproduces on PostgreSQL a tenant check that is present but not bound: with the RLS context set from the URL tenant a member of ACME reads Globex's accounts through correctly configured policies; membership first returns 404.
- Tests: 251 in the plugin plus 29 in the example. Evals: 47 cases (8 new), validated offline; no paid runs. Release details in [`docs/releases/RELEASE-NOTES-v1.7.md`](docs/releases/RELEASE-NOTES-v1.7.md).

## Extension v1.8 — Program Analysis Precision & Verified Remediation Foundations

The auditor classifies more call sites without inventing project edges, reads the structure of a loop before calling a commit accidental, attaches a structured remediation proposal it never applies, and compares two reports without ever saying "resolved". Everything from v1.0 to v1.7 remains; every option, rule id, fingerprint and report field is unchanged (report schema 1.6 is additive over 1.5).

- **Call classification.** Locals bound to library or builtin results (plain, awaited, unpacked, `with`), project functions returning builtin or library types, closures reading the enclosing function's typed names, ASGI `send`/`receive`/`app` callbacks, typed `Callable` parameters (v1.7 missed the generic form), lambdas that only forward to one target, bound-method references, `self.a.b.method()` through two typed links, same-module class annotations, mixins whose concrete subclasses agree on an attribute's kind, template-method hooks with one implementation, and unknown methods of classes with a third-party base. Loop targets and comprehension variables stay dynamic and say why: every dynamic site carries an origin (`uncertainty.dynamic_by_origin`), coverage is reported as three shares that sum to one, and `callback_params_unresolved` now counts parameters, not sites, and excludes framework callbacks. On the two calibration repositories dynamic sites fell by 18 % and 12 %, `not_found` by 37 % and 96 %, and every previously resolved edge kept its target.
- **Transactional precision.** `TX_COMMIT_IN_LOOP` classifies the loop by structure (`per_item`, `batch`, `paginated`, `convergence`, `retry`): per-item commits are reported as before with `details.loop_kind` and the evidence; structurally deliberate kinds are counted in `facts.commit_loops` and not reported (v1.7 silenced only the modulo form; names such as `BATCH_SIZE` prove nothing). New `TX_COMMIT_INSIDE_BEGIN_BLOCK` (high): `commit()` inside `with session.begin()`. Projects that require one transaction per use case opt in with `"transactions": {"single_transaction_per_use_case": true}` and get the deliberate loops reported too. 59 rules.
- **Remediation foundations.** Five rules carry a `remediation` object: problem, evidence, certainty (from the evidence state), proposed fix as steps, invariants, tests, regression risk. `status: proposed`, `applied: false`, `verified: false`, always; the auditor never edits code.
- **Report comparison.** `scripts/audit_diff.py BEFORE.json AFTER.json` and the richer `--baseline` block: new, no longer detected, persistent, severity/confidence/evidence-state changes, suppression transitions, partial-analysis reliability. A finding that is not reported any more is "no longer detected", never "resolved".
- **Measured.** `tests/fixtures/truth18` (`dev` scored in-process, `acceptance` scored once at release time): on `dev` the v1.7 engine scored F1 0.308, expected edges 1/6 and unresolved statuses 7/16; v1.8 scores 1.0, 6/6 and 16/16. Clivana (read-only) 55 → 52 findings: the three that disappeared are a convergence loop, a cursor-paginated job and a bounded batch deletion; AgentForge 11 → 15: four indirect-blocking findings in operator scripts (async code calling a worktree manager that shells out to git, followed through a two-level attribute chain); the resource question the first cut asked on a shared-token route was withdrawn once contracts record `principal_kind` (a `-> None` credential check identifies no caller). Time +6 % on the largest repository, memory flat. The reference implementation gains a PostgreSQL test for the new rule (30 tests); it corrected the rule's first draft. Release details in [`docs/releases/RELEASE-NOTES-v1.8.md`](docs/releases/RELEASE-NOTES-v1.8.md).

## Extension v1.9 — Verified Remediation & Safe Refactoring

The auditor proposes; `scripts/audit_remediate.py` applies the proposal **in an isolated copy per finding**, audits again and, when authorized for a trusted scope, runs the finding's linked reproducer before and after. The project itself is never written to; the human applies the diff. Everything from v1.0 to v1.8 remains (report schema 1.7 adds `remediation.recipe` / `automatable` / `tool`).

- **Trust model, fail closed.** Static steps execute no project code. Tests run only with `--authorize-execution`, a trust profile (`fixture`, `trusted`) with a scope that contains the project, and an executor that accepts the profile. The only executor shipped is the local process executor: process hygiene (copy without `.git`/environments, fresh `HOME`, reduced environment, no shell, allow-listed programs, closed stdin, timeout, CPU/file-size limits), **not a sandbox** (no network isolation, no filesystem confinement, no memory limit). `untrusted` projects (the default) are denied until an isolated executor is registered behind the same interface; `--trust` is recorded as an operator's declaration, never as a guarantee.
- **States that say what was checked, per finding.** `blocked`, `audit_clean` (finding gone, not suppressed, nothing new; no test executed), `verified` (plus at least one linked test that failed before and passed after, read from the runner's own nonce-bound report through an adapter, test files untouched, executor named; the model refuses it otherwise), `rejected` (stale baseline, candidate mismatch, finding persists or merely suppressed, tests modified, regression, reproducer failing after) and `inconclusive` (no reproducer, reproducer already passing, skipped or no tests, infrastructure error, timeout: never a defect of the patch). Each case carries its base digest, candidate id, reproducer runs with runner counts, both audits, invariants and how each was checked, regressions, limitations and history; a combined copy reports whether the candidates compose.
- **Three recipes, verified on PostgreSQL.** Remove a `commit()` inside `with session.begin()`, add the missing `await` on an `AsyncSession` coroutine (errors now surface: documented), move a per-item `commit()` after its loop (all or nothing: documented). Preconditions on the AST block nested commits, early returns, lambdas and shared lines; diffs are accepted by `git apply`. `tenant-ledger` ships five deselected reproducers; the tool verified the example's four findings on a throwaway cluster, one copy each, and verified their composition on one more copy against the regression suite of the corrected code (the test that documents the anti-pattern carries the `antipattern_demo` marker and keeps running in the default suite).
- **Adversarial controls.** Suppressing the finding instead of fixing it, editing a test, introducing an independent regression, a reproducer that already passed, a reproducer that fails after, skipped or missing tests, a stale baseline, a candidate for another file or base, infrastructure failures, timeouts, configuration errors and denied execution each end in a distinct state and reason code (`tests/test_audit_v19.py`).
- **Compatible.** The 59 rules, JSON, SARIF, fingerprints, suppressions, declared contracts, the opt-in loop policy and the comparison tool are unchanged; the v1.8 remediation proposal contract is intact; the remediation run is a separate artefact. Capability matrix and limits in `references/safe-refactoring.md`; release details in [`docs/releases/RELEASE-NOTES-v1.9.md`](docs/releases/RELEASE-NOTES-v1.9.md).

## Extension v2.0 — Architecture & Engineering Foundations (first increment: design engine)

FastAPI Architect now **designs and builds** backends as well as auditing and remediating them. `scripts/design_fastapi.py` reads a declarative, versioned architecture contract (`fastapi-architect/spec/1.0`), recommends a structure with its reasons, generates a runnable FastAPI project into an isolated directory, audits it with the existing engine and runs its tests through the v1.9 trust model. Nothing from v1.0 to v1.9 is rewritten; the 59 rules, JSON, SARIF, fingerprints, suppressions, contracts and the remediation run are unchanged (tool version 2.0.0, audit schema 1.7).

- **Contract, not conversation.** Functional modules and entities, scope, non-functional requirements, persistence, security and multi-tenancy, integrations, infrastructure, decisions, components and testing in plain JSON, validated strictly with every error at its JSON path (unknown keys, reserved names, cycles, inconsistent choices such as authorization without authentication). Every value carries its **provenance** (`explicit` or `assumed`); pending decisions and open questions travel into the report; a canonical digest ties the generated project to the exact contract.
- **A reasoned recommendation, never a default dogma.** Layered for a small scope; modular monolith once modules, scope or team justify boundaries (import-linter contracts generated); ports and adapters only in the modules that integrate with the outside; microservices only when independent deployment is stated and the team and module count justify it. Each rejected alternative is reported with its reason; an explicit preference is honoured with a warning; DDD, CQRS and event sourcing never appear as structure, only as pending decisions when the signals call for them.
- **Safe, deterministic generation.** App factory with lifespan, RFC 9457 problem details, pydantic-settings, API key compared in constant time, per-module schemas/models/repositories/services/dependencies/routers for the declared operations only, tenant scoping on every repository call, PostgreSQL through SQLAlchemy 2 async sessions with one commit per use case, tests through the HTTP boundary, and an `.audit-fastapi.json` that declares the design. Two runs produce identical bytes; re-running changes nothing; a non-empty target needs `--allow-existing` (missing files only); a differing file is never replaced without `--overwrite`; nothing is deleted; a manifest records every file's sha256 without timestamps.
- **Verified with the engines that already exist.** The generated project is audited (clean for the examples: the auditor now propagates router dependencies through `include_router`, with the dev/acceptance corpora scoring identically before and after) and its tests run only with `--authorize-execution` and a trust profile the executor accepts, in a copy of the project, with nonce-bound runner evidence. Both generated backends, `inventory-service` and `orders-platform` (hexagonal, PostgreSQL, multi-tenant, two integrations), are committed under `plugins/fastapi-architect/examples/generated/` and a test asserts each equals a fresh generation.
- **PostgreSQL for real (increment 2).** A PostgreSQL contract gets SQLAlchemy 2 models with a naming convention, foreign keys from declared `references` (composite `(tenant_id, id)` between tenant tables), an Alembic environment and an initial migration generated from the contract (tables exist only after `alembic upgrade head`, never through `create_all`), a request-scoped unit of work that rolls back on errors and answers constraint violations with 409, and a suite that only accepts an empty database, migrates it, truncates between tests, checks that the schema matches the models and repeats up/down. `orders-platform` was verified on a throwaway PostgreSQL 16 through the trust model (39 generated tests, nonce-bound evidence; a mutated model makes the drift test fail): `plugins/fastapi-architect/examples/generated/VERIFICATION.md`.
- **Tenant contracts that do not overpromise (increment 2).** Schema 1.1 adds `security.multi_tenant.binding`; only `api_key_registry` is generated (each key bound to one tenant, 403 for any other tenant, scoped queries, composite foreign keys). A multi-tenant contract without a binding is refused, and the report's `guarantees` block states declared versus implemented isolation (`shared_schema_application_scoping`; no row-level security, no resource authorization).
- **Route composition in the auditor (increment 2).** Route contracts carry `effective_paths` and `path_resolution` composed through `APIRouter(prefix=...)` and `include_router(..., prefix=...)`, including applications created in factories; `public_routes` and `route_auth_required` match them; a router mounted twice is public only if every mount is declared, a non-constant prefix is `unresolved`, fingerprints are unchanged, corpora score identically.
- **Safe project evolution (increment 3).** `scripts/evolve_fastapi.py discover` builds a static model of an existing FastAPI project (layout, routers and effective routes, models, services, repositories, dependencies, settings, lifespan, Alembic chain, file ownership against the manifest) without importing it; `plan` turns an evolution contract (`fastapi-architect/evolution/1.0`) into a previewable change set and verifies it in a copy; `apply` writes the project only after that verification, re-checking every digest. Generated projects grow by whole modules: integration points are regenerated from the merged contract, never merged by text replacement; a hand-edited integration point blocks the plan, other manual changes are preserved, historical migrations stay byte-identical and one additive revision is appended; the same contract twice is a no-op. `inventory-service` + suppliers (18 tests) and `orders-platform` + shipments (two chained revisions, 50 tests on a throwaway PostgreSQL 16) were verified through the trust model.
- **Safe evolution of existing modules and adoption of hand-written projects (increment 4).** Contract 1.1 adds `modify_modules`: nullable or defaulted fields, operations and dependencies on existing modules, classified `compatible` / `potentially_incompatible` (refused until a safe form is declared, such as a default for a NOT NULL column) / `unsupported` (removals, renames, type changes). PostgreSQL projects get additive column migrations (`add_column`, server defaults as the backfill, nullable foreign keys) verified on a throwaway PostgreSQL 16, with a backfill check and a migration-failure control. Hand-written projects are adopted module by module (`propose`, `adopt`, an adoption baseline, nothing else written) and then receive anchored structural edits (a handler appended to the router, an optional field appended to a class; comments and formatting preserved byte-for-byte; duplicates and ambiguity refused) while unadopted files stay protected. Every write goes through a recoverable writer: staged content and backups, a journal, atomic renames per file, `recover [--forward]` after an interruption, conflicts left alone; fault injection before, during and after publication is part of the suite.
- **Identity, authorization and tenant isolation (increment 5).** Schema 1.2 adds the security design contract: OIDC bearer tokens (`security.oidc`: issuer, audience, asymmetric algorithms only, JWKS with a bounded cache; the principal is built from verified claims; an unreachable provider is 503, not 401), API keys as grants (tenant, subject, roles), the tenant bound to the grant or to a verified claim (`principal_claim`), per-operation roles in route dependencies, ownership of `owned` entities enforced in the service and the repository query (403 for "visible but not yours", 404 for "not visible"), and PostgreSQL row-level security as an opt-in strategy (`shared_schema_rls`: ENABLE + FORCE policies in the creating migration, an application role verified not to bypass RLS, a transaction-local `set_config` context, two URLs). Impossible combinations are contract errors; a 1.0/1.1 contract keeps its meaning. `team-notes` (54 generated tests through the trust model) and `tenant-projects` (51 tests on a throwaway PostgreSQL 16 as the restricted role, 12 RLS demonstrations, concurrency, a refused BYPASSRLS role, a protected table added by evolution) are committed and verified; the auditor sees the authorization on every generated route and reports the stripped-route and session-`SET` negative controls. The security acceptance hardening that closes the area: the auditor no longer counts a dependency as authorization by its name (`require_admin` that decides nothing is `authorization_named_only`, seven negative controls, corpora and calibration projects unchanged), the generated JWKS client is verified against a local HTTPS provider (17 scenarios: TLS, rotation, size cap, retry floor, stale window, concurrency), the RLS threat model is written down and demonstrated (what a statement run as the application role can do that a bound parameter cannot), and every `guarantees` block is frozen as a regression fixture.
- **Honest limits.** Session authentication, refresh tokens, token issuance, public resources inside an authenticated API, cross-tenant administration, schema or database per tenant, outbox/inbox, service splitting, removals and renames in existing modules, expand/contract migrations, changes to existing handlers of adopted projects and layout migrations are pending decisions, not code; moving an existing project from application scoping to RLS is a new design, never an evolution; there is no atomicity across files, only a recoverable protocol; the local executor is process hygiene, not a sandbox. Contract and report schemas in `references/design-contracts.md`; design record in [`docs/architecture/design-engine-v2.md`](docs/architecture/design-engine-v2.md); release details in [`docs/releases/RELEASE-NOTES-v2.0.md`](docs/releases/RELEASE-NOTES-v2.0.md).
