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
        └── fastapi_audit/                # v1.4 auditor package: rules per category, facts, report, cli
plugins/fastapi-architect/evals/          # claude plugin eval suite (positive, negative, decision, tool-use cases)
plugins/fastapi-architect/examples/       # tenant-ledger: verified reference implementation (FastAPI, SQLAlchemy 2, Alembic, PostgreSQL RLS)
evals/evals.json                          # human-scorer prompts for evaluating the skill
scripts/audit_fastapi.py                  # root entry point forwarding to the skill's auditor
scripts/validate_structure.py             # packaging, links and version checks
tests/                                    # auditor suites per category, fixtures, PostgreSQL integration tests
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
