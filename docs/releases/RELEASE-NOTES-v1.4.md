# FastAPI Architect v1.4 — Database, Security & Architecture Auditor

Additive release on top of v1.3.0 (published at `74b779d`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, and the auditor command `python3 scripts/audit_fastapi.py <project>` with every v1.3 option.

## Auditor architecture
The single-file auditor is now a standard-library package next to the entry point, `plugins/fastapi-architect/skills/fastapi-architect/scripts/fastapi_audit/`:

| Module | Responsibility |
|---|---|
| `model.py` | Rule catalogue (severity, category, default verification), `Finding`, schema and tool versions |
| `discovery.py` | Read-only file walk, exclusions, symlink handling, include/exclude globs, environment classification (application, test, migration) |
| `suppressions.py` | `# noqa: ASYNC2xx` and `# audit-fastapi: ignore[RULE]` read with the tokenizer |
| `resolver.py` | Scopes, import aliases, shadowing, route decorators, annotation unwrapping (`Annotated`, `Optional`, `\|`) |
| `kinds.py` | Conservative typing of names: `Session`, `AsyncSession`, HTTP clients, from annotations, constructors and module-level factories |
| `facts.py` | Project pass: session factories and `Annotated` aliases, routers with dependencies and application-level auth signals, import-linter contracts, layers, business-modules root, import graph, optional `.audit-fastapi.json` |
| `rules/*.py` | One module per category: `async_rules`, `sqlalchemy_rules`, `transactions`, `migrations`, `security`, `architecture` |
| `engine.py` | Orchestration, test-code downgrade, suppressions, filters, summary |
| `report.py` | JSON, text and Markdown rendering; baseline comparison by fingerprint |
| `cli.py` | Command line, exit codes |

`audit_fastapi.py` (entry) and `scripts/audit_fastapi.py` (root shim) only forward. No runtime dependency was added; Python 3.9 to 3.13 are exercised by the tests.

## Rules added (30, in 5 categories)
- **sqlalchemy**: `SA_SESSION_MODULE_GLOBAL`, `SA_SESSION_SHARED_CONCURRENTLY`, `SA_ASYNC_SESSION_NOT_AWAITED`, `SA_SYNC_SESSION_IN_ASYNC_DEF`, `SA_ENGINE_CREATED_PER_CALL`, `SA_CREATE_ALL_AT_RUNTIME`, `SA_SESSION_NOT_CLOSED`. A global `sessionmaker`/`async_sessionmaker`/`scoped_session` is recognised as a factory and never reported; instances are. Factories imported under an alias and `Annotated[AsyncSession, Depends(...)]` aliases are resolved across files.
- **transactions**: `TX_COMMIT_IN_LOOP`, `TX_EXTERNAL_CALL_IN_TRANSACTION`, `TX_READ_MODIFY_WRITE` (always `not_verifiable_statically`, with the two-session reproduction recipe), `TX_COMMIT_WITHOUT_ROLLBACK` (not reported when the commit is bounded by a session context manager).
- **migrations**: `MIG_DROP_TABLE`, `MIG_DROP_COLUMN`, `MIG_DROP_CONSTRAINT`, `MIG_NOT_NULL_WITHOUT_DEFAULT`, `MIG_TYPE_CHANGE`, `MIG_INDEX_NOT_CONCURRENT`, `MIG_DDL_WITH_DML`, `MIG_DOWNGRADE_NOOP`, `MIG_RAW_SQL`. Indexes and seeds on tables created in the same migration, RLS enable/force statements, `CREATE POLICY/FUNCTION/EXTENSION` and `DROP FUNCTION/POLICY/TRIGGER` are not reported. Migrations are never executed.
- **security**: `SEC_TENANT_ID_FROM_REQUEST`, `SEC_RLS_SET_NOT_LOCAL`, `SEC_ROUTE_WITHOUT_AUTH_SIGNAL`. Auth signals are looked for on the handler, the route decorator, the router (locally and project-wide), the application (`include_router(dependencies=...)`, `FastAPI(dependencies=...)`, auth middleware). The findings say when a property is not verifiable statically and which negative test closes it.
- **architecture**: `ARCH_SERVICE_IMPORTS_FASTAPI`, `ARCH_DOMAIN_IMPORTS_INFRA`, `ARCH_REPOSITORY_IMPORTS_SERVICE`, `ARCH_CROSS_MODULE_INTERNAL_IMPORT`, `ARCH_CORE_IMPORTS_MODULE`, `ARCH_IMPORT_CYCLE`, `ARCH_IMPORT_LINTER_PRESENT`. Layers and modules root are inferred from paths (medium confidence) or configured (high). import-linter contracts are detected, listed, and downgrade the heuristic findings. Test files are excluded from the graph.

## Rules deliberately not implemented (backlog)
Lazy-loading strategy review, retries without idempotency keys, background tasks without authorization context, `SET LOCAL` outside an explicit transaction, hard-coded permissions, external identifier misuse, multiple commits per use case, long transactions. Each needs inter-procedural analysis or runtime data; shipping them as heuristics would add noise without evidence. Listed in `references/audit-rules.md`.

## Report and CLI (schema 1.2, additive)
- Per finding: `category`, `verification` (`static` | `needs_runtime_check` | `not_verifiable_statically`), `recommended_test`, `fingerprint` (stable across line shifts). `environment` gains `migration`.
- Top level: `files_reported`, `config` (effective options), `facts` (what was inferred: frameworks, factories, aliases, auth signals, import-linter, modules root, layers), `comparison` (with `--baseline`).
- Summary: `by_category`, `by_verification`.
- New options: `--format text|json|markdown`, `--category`, `--select`, `--ignore`, `--include-path`, `--exclude-path`, `--changed-files FILE|-`, `--baseline FILE`, `--fail-on-new`, `--exit-zero`, `--config FILE`. Every 1.0/1.1 field keeps its name, type and meaning; `findings_total` and `by_severity` still count suppressed findings; suppressions are unchanged.

## Calibration on real repositories (read-only)
Before fixture tests were written, the rules were run on two production codebases (Clivana, AgentForge) and the false positives found there were removed: indexes on tables created in the same migration (110 findings), routine RLS and function DDL, Alembic `env.py` engines, commits bounded by `async with factory() as session`, test helpers classified as `core`. Final counts: Clivana 51 findings (25 informational dynamic-SQL notes, 0 high), AgentForge 9. Neither repository was modified (snapshot digests and `git status` identical). Analysis time 4.5 s and 2.7 s.

## Tests
149 tests: the 64 of v1.3 (adapted to schema 1.2), 10 for the eval suite, and new suites per category over committed fixture projects (`tests/fixtures/`: correct and incorrect SQLAlchemy usage with aliases, transactions, an Alembic project with destructive, expand and contract revisions, router-protected and unprotected multi-tenant routes, a modular monolith with cycles, a hexagonal project with import-linter, a configured-layers project, Python 3.12+ syntax analysed from Python 3.9). `tests/integration/test_postgres_properties.py` starts a throwaway PostgreSQL cluster and reproduces the lost update versus `FOR UPDATE`, the session-level `SET` leak versus `SET LOCAL`, and the `NOT NULL` without default failure; it is skipped when `pg_ctl` or a psycopg driver is missing. Suites pass on Python 3.13 and 3.9 and under pytest.

## Documentation
New `references/audit-rules.md` (rule catalogue: proves / does not prove / how to verify, options, backlog). Extended: `architecture-audit.md` (categories, verification field, options), `data-layer.md` (sessions and engines), `async-concurrency.md` (lost updates, mechanisms table), `database-evolution.md` (expand-contract, backfills, NOT NULL, concurrent indexes, locks, rollback/roll-forward, rehearsal), `authz-multitenancy.md` (what static analysis can and cannot say), `architecture-decisions.md` (boundary checks and import-linter), `testing-matrix.md` (closing static findings with runtime tests). `SKILL.md` adds one routing row and one sentence; progressive disclosure preserved.

## Evals
Eight cases added to `plugins/fastapi-architect/evals/` (see its README): detect real session and transaction risks on a fixture, reject a false positive on a global `sessionmaker`, justify an existing hexagonal architecture, propose a concurrency test, accept a justified destructive migration, state that security is not verifiable with AST alone, select the database-evolution reference, and a negative Django case. The eleven v1.3 cases are unchanged.

Structural validation covers all nineteen cases offline. The paid regression sample for v1.4 was four cases, with-plugin arm only, one run each, default model, judge haiku: `negative-django-project` (skill not invoked, pass), `reject-false-positive-sessionmaker` (pass), `detect-session-risks` and `security-not-verifiable-statically`. The last two first scored 0.5 because the judge read labels literally (an answer opening with "no, not on this evidence" and a lost update called a "likely defect" while correctly stating nothing was reproduced); the two rubric clauses were rewritten to score substance and both cases then passed with the skill invoked and, for the fixture case, the auditor executed. Cost of the v1.4 sample including the re-runs: 2.16 USD.

## Known limitations
- No inter-procedural analysis; patterns are reported where written.
- Untyped sessions (`db=Depends(get_db)` without annotation) reduce precision to name-based heuristics.
- Layer inference is path-based; configure `.audit-fastapi.json` for other conventions.
- Security rules are review signals; a route protected outside the repository is a known false positive, and the finding says so.
- PostgreSQL integration tests require local `initdb`/`pg_ctl` and a psycopg driver.
