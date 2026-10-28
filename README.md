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
        └── audit_fastapi.py              # v1.3 read-only static auditor (stdlib only)
plugins/fastapi-architect/evals/          # claude plugin eval suite (positive, negative, decision, tool-use cases)
evals/evals.json                          # human-scorer prompts for evaluating the skill
scripts/audit_fastapi.py                  # root entry point forwarding to the skill's auditor
scripts/validate_structure.py             # packaging, links and version checks
tests/test_audit_fastapi.py               # auditor test suite (unittest / pytest)
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
