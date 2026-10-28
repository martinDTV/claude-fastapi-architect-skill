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
    └── assets/
        ├── pyproject.toml                # uv, ruff, mypy, pytest, import-linter configured
        ├── Dockerfile                    # multi-stage, uv, non-root
        ├── dockerignore.txt
        ├── pre-commit-config.yaml
        └── adr-template.md
evals/evals.json                          # test prompts for evaluating the skill
```

## Principles

1. Optimize for change, not for imagined scale.
2. Boundaries before distribution.
3. Every layer must pay rent.
4. Reversible decisions fast; irreversible ones get an ADR.

## Contributing

Issues and pull requests are welcome. Any code added to `references/` must pass `ruff format --check`, `ruff check`, `mypy --strict` and, where applicable, tests against PostgreSQL. Run `claude plugin validate .` before opening a PR that touches the manifests.

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
