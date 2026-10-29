# FastAPI Architect v1.5 — Interprocedural Analysis & Verified Reference Implementation

Additive release on top of v1.4.0 (`d40af02`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, the auditor command and every v1.4 option. Report schema 1.3 is a superset of 1.0, 1.1 and 1.2.

## Interprocedural engine
Two new standard-library modules in `fastapi_audit`:

| Module | Responsibility |
|---|---|
| `summaries.py` | One record per function: parameters and declared kinds, call sites with argument flow, blocking calls, writes, session creation, tenant-context markers, `Depends` targets, return/yield kinds of dependencies, retry shape, idempotency signals, project-class instances |
| `callgraph.py` | Conservative call graph over the summaries: imported and module-level functions, `self.method`, instances of project classes, methods through declared bases, source-root aliases (`app.x` == `src.app.x`); kind propagation from dependencies (verified) and callers (inferred); resolution statistics |

Explicit limits, all reported in the new `interprocedural` block: `--max-call-depth` (default 4 project functions after the caller), a wall-clock budget (truncation is reported, never fatal), bounded propagation rounds, visited sets for recursion and cycles. `--no-interprocedural` restores v1.4 behaviour. Unresolved calls are counted as `dynamic`, `external`, `not_found` or `constructors` so the reader sees how much of the project the graph covers. Nothing is executed; resolution is syntactic only.

## Evidence classification (schema 1.3)
- Per finding: `evidence` = `verified` (pattern and symbols resolved from the AST) | `inferred` (a kind was propagated from a caller or guessed from a name; confidence lowered one level and the message says so) | `unknown` (the finding is a question). Cross-function findings carry `call_path`.
- Existing rules benefit: `SA_ASYNC_SESSION_NOT_AWAITED`, `SA_SYNC_SESSION_IN_ASYNC_DEF`, `SA_SESSION_SHARED_CONCURRENTLY`, `TX_COMMIT_IN_LOOP` and `TX_COMMIT_WITHOUT_ROLLBACK` now reach parameters typed through `Depends(f)` return/yield types, through callers, and through `factory: async_sessionmaker[...]` annotations.

## Rules added (6; 41 in total, 9 categories)
- `ASYNC_BLOCKING_CALL_INDIRECT` (async): blocking reached through synchronous project helpers, with the call path; not reported in tests; one finding per helper per file.
- `TX_NESTED_BEGIN` (transactions): `session.begin()` inside an open `begin()` on the same session.
- `SA_ASYNC_LAZY_LOAD_RISK` (sqlalchemy): relationships with an implicit lazy strategy accessed under `AsyncSession` on objects loaded without loader options; silent for `lazy="selectin"`/`"raise"`, explicit options and sync sessions.
- `RETRY_NON_IDEMPOTENT_WRITE` (reliability, new category): retry decorator or attempt loop around a database/HTTP write with no idempotency signal; poll loops and read-only retries are not reported.
- `BG_REQUEST_SCOPED_RESOURCE` (reliability): dependency-provided session handed to `BackgroundTasks.add_task` or a task created in the handler.
- `BG_TASK_CONTEXT_UNVERIFIED` (reliability, info): background work that writes without captured or recreated tenant/principal context, reported only when the spawning function holds request context; classified captured / recreated / unverifiable.

Precision work alongside: retry and poll loops are no longer reported as commit-in-loop; auth dependencies are recognised through `Annotated` aliases re-exported from facades and across source-root prefixes (`OwnerDep`, `MemberDep`); resource ids such as `account_id` are no longer tenant parameters.

## Rules deferred (backlog, documented in `references/audit-rules.md`)
`SET LOCAL` outside an explicit transaction, hard-coded permissions, external identifiers, multiple commits per use case, long transactions, outbox/inbox detection, resolution through object attributes and callbacks, decorators that replace functions, lazy-loading strategy for sync N+1.

## Reference implementation: `plugins/fastapi-architect/examples/tenant-ledger` (label `verified`)
FastAPI 0.162, Pydantic 2.14, SQLAlchemy 2.0.55 (asyncpg), Alembic 1.20, PostgreSQL 16.10. Stage 1 modular monolith (`tenants`, `ledger` modules with a `public.py` facade), engine and `async_sessionmaker` in the lifespan, one session per request, membership-based authorization binding the tenant in the path to the principal (owners only for deletes, 404 for non-members), PostgreSQL row-level security enforced on a non-owner application role with a transaction-local `set_config('app.tenant_id', ..., true)` and fail-closed policies, atomic balance updates plus a `CHECK` constraint, `ETag`/`If-Match` optimistic renames (428/412), idempotency keys, one commit per use case, RFC 9457 errors, two migrations (schema; RLS and grants) with downgrade. Demo authentication (`X-Demo-User`) is marked as not for production.

22 tests on a throwaway PostgreSQL cluster started by the suite: authorized and denied operations, cross-tenant denial through the API and below it (direct SQL as the app role: context A sees A only, no context sees nothing, cross-tenant INSERT rejected), 50 concurrent deposits without a lost update, overdrafts under concurrency, If-Match races with exactly one winner, rollback after an injected failure, idempotent replays and concurrent duplicates, migrations from an empty database and from the previous revision plus downgrade and `alembic check`, and the reproduction of two anti-patterns (read-modify-write losing a deposit; `MissingGreenlet` from an implicit lazy load) next to their fixes. `VERIFICATION.md` records versions, commands, results and the two defects found while verifying (RLS policy cast after rollback; reading ORM rows after commit/rollback without tenant context). `LIMITATIONS.md` lists what the example does not cover. The auditor reports only the two deliberate anti-patterns.

Dogfooding the example found and fixed two auditor false positives (auth aliases through facades; `account_id` as tenant parameter), recorded in the commit history.

## Tests
173 in the plugin (149 from v1.4, 21 for the engine and the new rules on the `interproc` and `hexagonal_sessions` fixtures, 3 regression tests for the alias and factory fixes) plus 22 in the reference implementation. Suites pass on Python 3.13 and 3.9 (PostgreSQL integration tests skipped on 3.9 for lack of a driver) and under pytest. Determinism: reports are byte-identical across runs; the engine's timing is not serialised.

## Calibration on real repositories (read-only)
Clivana (457 files): 3,917 functions, 27,310 call sites, 9,050 resolved project edges, 10,726 dynamic and 637 not-found sites counted, 24 parameters typed from callers, analysis in about 7 s, no truncation; 51 findings, the same total as v1.4 (49 verified, 2 inferred), no v1.5 rule fired; during calibration a worker poll loop and its commit were briefly reported as retry/commit-in-loop and the rules were corrected. AgentForge (288 files): 2,096 functions, 14,996 call sites, 3,240 project edges, about 4 s; 9 findings as in v1.4, all verified; 75 indirect-blocking findings on async tests were removed by excluding test code from that rule. Neither repository was modified (working-tree status identical before and after).

## Documentation
New `references/audit-engine.md` (engine architecture, evidence, limits, false positives and negatives). Extended `audit-rules.md`, `architecture-audit.md`, `data-layer.md`, `async-concurrency.md`, `reference-implementation-strategy.md` (status: first verified example). SKILL.md adds one routing row and one sentence.

## Evals
Twelve cases added (31 total): session flow across modules, indirect blocking helper, shared session in a task group, async lazy loading, non-idempotent retry, request session in background tasks, tenant propagation into a worker, lost response double charge, incremental refactor of a global session, a decision not to add complexity, and two negatives (standalone SQLAlchemy ETL script, Node.js/Prisma). Rubrics judge substance. Structural validation covers all 31 cases offline; no paid evaluation runs were made for v1.5, in line with the budget instruction.

## CLI and JSON compatibility
New options: `--max-call-depth N`, `--no-interprocedural`. New report fields: `interprocedural` (top level), `evidence` and `call_path` (per finding), `reliability` in `by_category`. All 1.0–1.2 fields keep their names, types and meaning; baselines produced by v1.4 remain comparable (fingerprints unchanged).

## Known limitations
- Resolution stops at object attributes, callbacks, framework dispatch and decorators that replace functions; such calls are counted, not followed.
- Parameters injected without annotation and without a resolvable dependency remain untyped (name-based low-confidence signals).
- Write detection in retries covers session writes and HTTP mutating verbs on known clients; writes through other libraries are not seen.
- The reference implementation is verified on the stack recorded in `VERIFICATION.md` only.
