# FastAPI Architect v1.6 — Advanced Program Analysis, Security Verification & Reliability Engineering

Additive release on top of v1.5.0 (`adab3d3`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id, the suppression markers and the fingerprints. Report schema 1.4 is a superset of 1.0–1.3: baselines produced by v1.5 still compare.

## Diagnosis that drove the work
Before touching the engine, unresolved call sites of two real repositories were classified by syntactic shape (read-only). The causes were concentrated: builtins counted as unknown, methods on `self.<attr>` assigned in `__init__` from typed parameters, names re-exported through `__init__.py`/`public.py` facades, relative imports bound as "origin unknown", parameters typed with library classes, and a parameter shadowing a module-level definition that was resolved to the wrong target. Each became a resolution step with a ground-truth marker before any detector ran.

## Call graph (`summaries.py`, `callgraph.py`, `resolver.py`, `facts.py`)
- Relative imports resolve to absolute names from the file's module; module-level imports are recorded as re-exports and followed by a bounded, cycle-safe `canonical()`; Annotated aliases, factories and protected routers are registered under every name a module can be imported as (`src.app.x` and `app.x`).
- Instance attributes typed in `__init__` (`self._session = session`, `self._repo = Repo(...)`, class-level annotations) carry kinds and classes into every method; locals bound to project-call results use the callee's annotated return class; functions handed as arguments resolve only when every caller agrees; Protocols and abstract classes resolve to their single implementation (nominal or structural) with `inferred` evidence and stay `ambiguous` otherwise; static and class methods, module-level instances and `None` placeholders are handled.
- Every call site is classified: `project`, `external`, `builtin`, `object`, `constructors`, `ambiguous`, `not_found`, `dynamic`. A library sharing a directory name with the project is external, not not-found.
- Edges carry their kind (`direct`, `attribute`, `return`, `callback`, `interface`) and evidence; blocking paths and transaction paths propagate the weakest evidence on the chain.

## Resource flow
Session and client kinds now flow through facades, aliases in src layouts, instance attributes, constructor arguments (then `inferred`) and callbacks. A shared `httpx.AsyncClient` is still not reported. A shared `AsyncSession` is reported when two coroutines scheduled together use it directly, through a parameter, or through two methods of the same object (`call_path` names the methods and the attribute).

## Rules (48 in total, 9 categories; six new, seven extended)
New: `ASYNC_TASK_REFERENCE_DROPPED`, `RETRY_UNBOUNDED`, `REL_HTTP_CALL_WITHOUT_TIMEOUT`, `REL_DUAL_WRITE_WITHOUT_OUTBOX`, `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` (a question, `evidence: unknown`), `SEC_PRIVILEGE_CHANGE_UNVERIFIED` (a question), plus `ANALYSIS_PARTIAL` for files where a rule module or the summary pass hit a resource limit.
Extended: `SA_*`/`TX_*` rules reach `self.<attr>` sessions and clients; `SA_SESSION_SHARED_CONCURRENTLY` through methods of one object; `TX_EXTERNAL_CALL_IN_TRANSACTION` through resolved call paths; `ASYNC_BLOCKING_CALL_INDIRECT` through interfaces and callbacks (then `inferred`, lower confidence); `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` recognises authentication reached through composed dependencies; `BG_REQUEST_SCOPED_RESOURCE` sees sessions typed through aliases.
Tightened from real false positives: `TX_COMMIT_WITHOUT_ROLLBACK` only for handlers that can catch a commit failure (bare, `Exception`, DBAPI/SQLAlchemy families), `REL_DUAL_WRITE_WITHOUT_OUTBOX` skips project-owned queues that stay in the database and in-process dispatchers, `REL_HTTP_CALL_WITHOUT_TIMEOUT` ignores httpx clients (default timeout) and only names `requests` receivers, privilege verbs require a role/permission/access object.

## Security and concurrency semantics
Nothing is called a vulnerability for lack of a function named `authorize`: dependency chains are followed three levels, and when the binding between the requested tenant and the authenticated principal is not visible the finding says so with `evidence: unknown` and a cross-tenant test. `asyncio.gather` alone is never a finding; the session has to be the shared object. The reference implementation shows on PostgreSQL that a shared `AsyncSession` does not raise with asyncpg (statements are serialised by a lock): the two units silently share one transaction and the unit that rolls back discards the other's write.

## Ground truth, precision, recall (Phase 7)
`tests/fixtures/truth16` carries its expectations as inline markers (`truth`, `truth-absent`, `truth-edge`, `truth-unresolved`) written before the detectors ran; one marker was corrected during the work (`dict` attribute receiver: `builtin` is more precise than `dynamic`) and is noted here. `scripts/audit_precision.py` scores the current engine and any previous commit in separate processes:

| Corpus metric | v1.5.0 | v1.6.0 |
|---|---|---|
| True / false positives, false negatives | 1 / 1, 14 | 15 / 0, 0 |
| Precision / recall / F1 | 0.5 / 0.067 / 0.118 | 1.0 / 1.0 / 1.0 |
| Expected edges resolved | 2/7 | 7/7 |
| Expected uncertain sites kept uncertain | 0/4 | 4/4 |
| Classified sites | 0.448 | 0.979 |

The corpus was built alongside the detectors, so the real test is the regression diff and the field calibration below.

## Calibration on real repositories (read-only, working trees unchanged)

| | Clivana (457 files) v1.5 → v1.6 | AgentForge (288 files) v1.5 → v1.6 |
|---|---|---|
| Call sites | 27,310 | 14,996 |
| Project edges | 9,050 → 9,778 | 3,240 → 4,771 |
| Unresolved (dynamic + not found) | 41.6% → 12.8% | 58.6% → 18.5% |
| Project-call coverage | 0.934 → 0.974 | 0.725 → 0.969 |
| Classified sites | 0.607 → 0.881 | 0.497 → 0.821 |
| Ambiguous sites | — → 18 | — → 57 |
| Findings | 51 → 63 (+4 retried writes, +6 commits in loop on attribute sessions, +2 privilege-change questions) | 9 → 11 (+1 retried HTTP write, +1 dropped task reference) |
| Findings removed | 0 | 0 |
| Audit wall time, best of two back-to-back | 10.1 s → 11.1 s | 5.9 s → 6.4 s |
| Peak RSS (measurement harness, audit plus a second facts pass) | 88 MB → 105 MB | 51 MB → 59 MB |

Thirty-three further findings surfaced during calibration (domain-exception handlers after commits, project-owned job queues, consent revocations as privilege changes, httpx clients without explicit timeout) and were judged false positives; the four tightenings above removed them. On the eleven existing fixtures the diff against v1.5 is +2 findings (a bare `create_task` in the interproc fixture, a `requests.post` without timeout in the transactions fixture) and no removal.

## Robustness (Phase 5)
Parser limits (`FILE_SKIPPED` for recursion/memory, `PY_SYNTAX` for nesting the grammar rejects) were already handled; v1.6 adds a per-rule-module recursion guard (`ANALYSIS_PARTIAL`, the other modules still run), a summary-pass skip recorded in `interprocedural.files_skipped`, a `--time-budget` option (truncation reported, `partial_analysis` true), and an `interprocedural.limits` block. Determinism holds for the same input; a wall-clock truncation is by nature not reproducible and the report says so. A synthetic repository of 1,801 functions resolves fully in well under the budget. Symlinks outside the root, unreadable files and oversized files keep their v1.3 behaviour.

## Reference implementation
`examples/tenant-ledger`: five adversarial tests on a throwaway PostgreSQL 16.10 (27 tests in total, ruff clean): shared session mixing two transactions versus one session per task; request-scoped session handed to a task losing its tenant context (row-level security refuses the write) versus a task with its own session; cancellation by timeout after an UPDATE leaving nothing committed and the pool usable; a lost response retried without an idempotency key (applied twice) and with one (replayed); RLS configuration invariants (the application role is not superuser, cannot bypass RLS or create roles, owns no table; tenant tables force RLS). `VERIFICATION.md` renewed; the auditor reports exactly the four deliberate anti-patterns plus one informational note.

## Tests
207 in the plugin (173 from v1.5 plus 34 in `tests/test_audit_v16.py`: corpus scored in-process, resolution details, each rule's positives and negatives, robustness, schema 1.4 and CLI contracts) and 27 in the example. Suites pass on Python 3.13 and 3.9 (PostgreSQL integration tests skipped on 3.9 for lack of a driver) and under pytest. Two older tests were updated additively (resolution keys may grow; the timeout rule adds one finding to a v1.3 determinism fixture).

## Evals
Eight cases added (39 total): indirect dependency through an attribute, insufficient evidence on tenant binding, two apparently insecure patterns that are correct, a double charge after a retry, authorization through abstractions, the modular-monolith decision, an adversarial request to suppress two documented findings, and a Django/Celery negative. Structural validation covers all 39 offline; no paid evaluation runs were made.

## Compatibility
New CLI option `--time-budget`. New report fields: `resolution.builtin/object/ambiguous`, `interprocedural.edges/coverage/uncertainty/limits/attribute_kinds/files_skipped`, `config.time_budget_s`, rule `ANALYSIS_PARTIAL`. Everything from 1.0–1.3 keeps its name, type and meaning.

## Known limitations
- Attribute chains deeper than one level, callbacks supplied by the framework, `getattr`, decorators that replace functions and dynamic imports remain unresolved and are counted.
- Protocols with several implementations are deliberately ambiguous; no finding is built on a guess between candidates.
- Tenant-binding and privilege-change findings are questions; middleware, RLS keyed on the principal or a check inside the service make them false positives, which only a negative test can show.
- A wall-clock truncation is not reproducible; rerun with a larger budget before comparing with a baseline.
- The reference implementation is verified on the stack recorded in `VERIFICATION.md` only; the cancellation scenario is demonstrated between statements, not inside a driver operation.
