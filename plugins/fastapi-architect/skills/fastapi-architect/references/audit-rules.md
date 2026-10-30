# Static audit rules: what each one proves, what it cannot, and how to verify

Catalogue for `scripts/audit_fastapi.py` (v1.7, report schema 1.5; additive over 1.0–1.4). Read it when interpreting a report, deciding whether a finding is a defect, or explaining why the tool stayed silent. `--list-rules` prints the short form.

Every finding carries `verification`:

| Value | Meaning | What closes it |
|---|---|---|
| `static` | The pattern itself is the evidence (a coroutine never awaited, a table dropped) | Reading the code confirms or refutes it |
| `needs_runtime_check` | The pattern is real; its impact depends on load, data or configuration | A measurement or a test on a real database |
| `not_verifiable_statically` | The property (a membership check, a race) cannot be decided from source | A negative or concurrent test; the finding is a question |

Test code is classified (`environment: test`), never hidden unless `--exclude-tests`; blocking, session and transaction findings in tests drop one confidence level. Alembic revisions and `env.py` are `environment: migration`. Documented suppressions (`# noqa: ASYNC2xx`, `# audit-fastapi: ignore[RULE]`) stay in the report as `suppressed`.

## Categories and options

```bash
python3 scripts/audit_fastapi.py <project> --category sqlalchemy,transactions   # subset
python3 scripts/audit_fastapi.py <project> --format markdown                    # report for a PR
python3 scripts/audit_fastapi.py <project> --baseline last.json --fail-on-new   # gate on regressions
python3 scripts/audit_fastapi.py <project> --changed-files <(git diff --name-only main)
```

`.audit-fastapi.json` at the project root (optional, read-only) may declare `layers` (layer name to path globs), `modules_root`, `exclude`, `include_paths`, `exclude_paths`, `ignore_rules`. The `facts` block of the report states what was configured and what was inferred.

## async and routes (v1.3)

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `ASYNC_BLOCKING_CALL` | A known blocking callable (`time.sleep`, `requests.*`, `subprocess.run`, sync `httpx` helpers, sync drivers) runs directly in an `async def` | That the path is hot or the call long | Concurrent latency test or event-loop lag |
| `ASYNCIO_RUN_NESTED` | `asyncio.run`/`run_until_complete` inside `async def` | That the function runs under a loop in production | Call it from a running loop; expect `RuntimeError` |
| `ROUTE_NO_RETURN_ANNOTATION` | No return annotation and no `response_model`/`response_class` | A defect; informational only | Review what the route returns |

Blind spots: calls through attributes (`self.client.get`), helpers that block, routes registered with `add_api_route`.

## sqlalchemy

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `SA_SESSION_MODULE_GLOBAL` | A `Session`/`AsyncSession` instance is bound at module level (constructor, or a call to a module-level `sessionmaker`/`async_sessionmaker`, also when imported under an alias). Factories and `scoped_session` registries are not reported | That the global is used concurrently | Two concurrent requests through it; look for identity-map and connection sharing |
| `SA_SESSION_SHARED_CONCURRENTLY` | One session-typed name is passed to several coroutines in `asyncio.gather`/`wait`, or to `create_task`/task groups/executors/threads | That the callees overlap in time (`create_task` followed by an immediate `await` is sequential) | Run two of the coroutines together on a real database; expect the concurrent-operation error or interleaved state |
| `SA_ASYNC_SESSION_NOT_AWAITED` | A coroutine method of a name typed `AsyncSession` (`execute`, `commit`, `flush`, `get`, `scalar`...) is called without `await` | Nothing more: this is a bug when the type is right | Read the annotation; run the code and look for "coroutine was never awaited" |
| `SA_SYNC_SESSION_IN_ASYNC_DEF` | A name typed `Session` does database I/O inside `async def` (high); `.query()` on an untyped session-like name (medium); `.commit()`/`.execute()` on an untyped session-like name without `await` (low: sync session or missing await) | Which of the two the low-confidence case is | Check the dependency that provides the name |
| `SA_ENGINE_CREATED_PER_CALL` | `create_engine`/`create_async_engine` inside a route handler (high) or an ordinary function not named like a factory and not cached (medium) | That the function is called often | Count pools/connections under load |
| `SA_CREATE_ALL_AT_RUNTIME` | `metadata.create_all()` in application code (not tests, not migrations) | That it runs in production | Check the startup path and whether Alembic owns the schema |
| `SA_SESSION_NOT_CLOSED` | A session built from a factory is neither closed, used as a context manager, returned, yielded-with-close nor handed to another call | That the connection leaks in practice (GC may close it) | Pool checked-out count after requests |

Kinds come only from annotations (`Session`, `AsyncSession`, `Annotated[...]` aliases defined at module level anywhere in the project), constructors, module-level factories and `with Factory() as s`. A session injected as `db=Depends(get_db)` without an annotation has unknown type; rules then report with low confidence or stay silent.

## transactions

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `TX_COMMIT_IN_LOOP` | `commit()` on a session (typed or session-like name) inside a loop body | That one transaction is the intended semantics; batching can be deliberate | Make a later iteration fail; decide whether earlier rows must survive |
| `TX_EXTERNAL_CALL_IN_TRANSACTION` | HTTP module helpers, typed HTTP clients or sleeps inside a `with X.begin()` block | Duration of the lock | Stall the external call; watch lock waits and pool exhaustion |
| `TX_READ_MODIFY_WRITE` | A field is derived from its loaded value (`obj.x += ...`, `obj.x = obj.x + ...`) after a session load, with no `with_for_update` in the function | A race: static analysis cannot show concurrency | Two sessions, same row, both commit; the integration tests under `tests/integration` show the recipe |
| `TX_COMMIT_WITHOUT_ROLLBACK` | A `try` whose body commits on a session that outlives the block and whose handler neither rolls back nor re-raises | Anything when the commit is inside `with factory() as s` (the context manager closes it): not reported | Trigger the failure; the next statement raises `PendingRollbackError` |

Guidance (the rules point at it, they do not prescribe it): `SELECT ... FOR UPDATE` for short critical sections on hot rows; `version_id_col` or `If-Match` for optimistic control on user-edited records; atomic `UPDATE ... SET x = x + :d` for counters; unique constraints for "create if absent"; isolation levels only after measuring; deadlock strategy documented when several rows are locked; retries only on idempotent writes. Not every operation needs a lock: a race that cannot happen (single writer, append-only) is not a defect.

## migrations

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `MIG_DROP_TABLE` / `MIG_DROP_COLUMN` | `upgrade()` drops a table or column | That it is wrong: it is the contract step of expand-contract when no reader or writer remains | Old release against the new schema; backup or archive exists |
| `MIG_DROP_CONSTRAINT` | A constraint or index is dropped (low when the same migration creates a constraint on the table) | Which invariant depended on it | Search the code for the invariant; downgrade path |
| `MIG_NOT_NULL_WITHOUT_DEFAULT` | `add_column(nullable=False)` without `server_default`, or `alter_column(nullable=False)` (lower confidence when an `UPDATE` precedes it in the same file) | That the table has rows | Apply to a populated copy |
| `MIG_TYPE_CHANGE` | `alter_column(type_=...)` | Whether PostgreSQL rewrites the table for this conversion | Time it on a production-sized copy; run old and new code |
| `MIG_INDEX_NOT_CONCURRENT` | `create_index` (or raw `CREATE INDEX`) without `CONCURRENTLY` on a table that this migration did not create | Table size; a small table is fine | Build it on a copy while a writer runs |
| `MIG_DDL_WITH_DML` | Schema operations and data statements (`UPDATE`/`INSERT`/`DELETE`/`bulk_insert`) in one `upgrade()`, excluding seeds of tables created in the same migration | Row counts | Time it; watch lock waits |
| `MIG_DOWNGRADE_NOOP` | `downgrade()` is `pass` or raises | That rollback is needed | Document roll-forward |
| `MIG_RAW_SQL` | Raw `ALTER TABLE`, `DROP TABLE/INDEX/SCHEMA`, `TRUNCATE`, `LOCK`, `VACUUM`, `CLUSTER`, `REINDEX`; dynamic SQL once per file (info). `ENABLE/FORCE ROW LEVEL SECURITY`, `CREATE POLICY/FUNCTION/EXTENSION`, `DROP FUNCTION/POLICY/TRIGGER` are not reported | Lock level or duration | Read the statement; `EXPLAIN` is not available for DDL, so rehearse |

The auditor never runs migrations. Rehearse on a disposable database created from a production-like dump, and on an empty one.

## security

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `SEC_TENANT_ID_FROM_REQUEST` | A route uses a tenant/organisation parameter from the request and no authentication or membership dependency is visible on the handler (`Depends`/`Security` with an auth-looking name, auth-looking annotation or alias), its router (`APIRouter(dependencies=...)` locally or elsewhere), the route decorator or the application (`include_router(dependencies=...)`, `FastAPI(dependencies=...)`, auth middleware) | That no check exists: middleware, gateways and composed dependencies are invisible | Cross-tenant negative test on this route for single resource, list, search and aggregate |
| `SEC_RLS_SET_NOT_LOCAL` | SQL text with `SET <dotted.setting> = ...`, `SET ROLE` or `set_config(..., false)` without `LOCAL` | That the connection is pooled (it almost always is) | Same connection: set, commit, `SHOW` in the next transaction |
| `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` | No auth signal visible for a non-health route (informational) | Anything about intent | Call without credentials; assert the documented policy |

Silence is not safety: a visible `Depends(get_current_user)` proves authentication, not that `tenant_id` is bound to that user. Multi-tenancy is verified by negative tests, not by the auditor. See `references/authz-multitenancy.md`.

## architecture

Layers are inferred from paths (`router.py`/`routers/`/`api/`, `service.py`/`services/`/`use_cases/`/`application/`, `repository.py`/`repositories/`/`persistence/`, `domain/`/`entities/`/`ports.py`, `adapters/`/`infrastructure/`, `core/`/`shared/`/`utils/`) unless `.audit-fastapi.json` declares them; the business-modules root is the shallowest `modules/` or `features/` directory with two or more packages. Inferred layers give medium confidence, configured layers high. Test files are never part of the architecture graph.

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `ARCH_SERVICE_IMPORTS_FASTAPI` | A service/repository-layer file imports `fastapi`/`starlette` | That the file is business logic (vertical slices put handlers in `service.py` sometimes) | Confirm the layer; configure `layers` if the convention differs |
| `ARCH_DOMAIN_IMPORTS_INFRA` | A domain-layer file imports an ORM, HTTP client, broker or cloud SDK | | |
| `ARCH_REPOSITORY_IMPORTS_SERVICE` | Repository imports service (resolved to a project file) | | |
| `ARCH_CROSS_MODULE_INTERNAL_IMPORT` | Module A imports a file of module B other than `public.py`/`__init__.py` (module-level or function-level) | That the project adopted the `public.py` convention | Compare with the project's own rule; import-linter contracts win |
| `ARCH_CORE_IMPORTS_MODULE` | A core/shared file imports a business module | | |
| `ARCH_IMPORT_CYCLE` | Module-level imports form a cycle (one finding per strongly connected component) | That it breaks at runtime (function-level imports are excluded) | `python -X importtime`; break the cycle |
| `ARCH_IMPORT_LINTER_PRESENT` | `.importlinter`, `setup.cfg` or `pyproject.toml` configures import-linter; contract names listed | | Run `lint-imports`; it is authoritative and the findings above drop one confidence level |

Architectures are not forced: a hexagonal layout with `domain/`, `application/`, `adapters/` and `api/` and no cross-module violations produces no findings; microservice repositories without those directories are quiet; vertical slices under `features/` are checked only for cross-slice imports.

## When the tool cannot determine a property

- A session injected without an annotation: `SA_*` rules stay silent or report low confidence. Add the annotation (`SessionDep`) to get precise results.
- A route protected by a gateway or middleware the project does not contain: `SEC_*` findings are expected and must be closed with a negative test, then suppressed with `# audit-fastapi: ignore[SEC_ROUTE_WITHOUT_AUTH_SIGNAL]` if the policy is permanent.
- A race condition: `TX_READ_MODIFY_WRITE` is a question. The integration test recipe answers it in seconds on a disposable PostgreSQL.
- Code the interpreter cannot parse (newer syntax): `PY_SYNTAX`, partial analysis, rerun with the project's Python.
- Dynamic SQL in migrations: reported once per file as information; read it.

## Interprocedural rules (v1.5)

Since v1.5 each finding also carries `evidence`: `verified` (pattern and symbols resolved from the AST), `inferred` (a kind was propagated from a caller or a dependency, or guessed from a name; confidence is lowered one level) or `unknown` (the finding is a question). Cross-function findings carry a `call_path`. `references/audit-engine.md` explains the engine and its limits.

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `ASYNC_BLOCKING_CALL_INDIRECT` | An `async def` calls a synchronous project function that reaches a blocking call or sync `Session` I/O along a resolved call path (depth ≤ `--max-call-depth`) | That the caller never offloads the helper at a level the engine did not see | Concurrent latency test; read the functions in the `call_path` |
| `TX_NESTED_BEGIN` | `with x.begin()` opened inside a block that already began a transaction on `x` | Nothing more; SQLAlchemy raises on this | Run the function; `InvalidRequestError` |
| `SA_ASYNC_LAZY_LOAD_RISK` | In an `async def`, an attribute that is a relationship with implicit lazy strategy is accessed on an object loaded in that function without loader options | That the attribute is not already populated | Load the object through `AsyncSession` and touch the attribute: `MissingGreenlet`; then `selectinload`/`lazy="raise"` |
| `RETRY_NON_IDEMPOTENT_WRITE` | A function under a retry decorator or an attempt loop performs (or reaches within two calls) a database or HTTP write and shows no idempotency signal (key header, dedup/request id, `ON CONFLICT`) | That the write is not idempotent by database design | Fault-injection: apply the write, drop the response, retry, assert one effect |
| `BG_REQUEST_SCOPED_RESOURCE` | A route hands a dependency-provided session to `BackgroundTasks.add_task` or a task it creates | Whether the task finishes before the response in practice | Let the task outlive the response; closed-connection errors and pool checkout |
| `BG_TASK_CONTEXT_UNVERIFIED` | From a function holding request context, a task is spawned whose target writes and neither receives principal/tenant/session context nor recreates one visibly | Anything about authorization inside the task (informational, `unknown`) | Trigger the task as tenant A with B's data reachable |

Kinds propagated through `Depends(f)` return/yield types count as `verified`; kinds propagated from callers count as `inferred`. A shared `httpx.AsyncClient` is not reported: HTTP clients are designed for concurrent reuse, sessions are not.

## Rules added or extended in v1.6 (schema 1.4)

The engine now resolves relative imports, facade re-exports, instance attributes typed in `__init__`, annotated return types, functions passed as arguments (when every caller agrees) and single-implementation Protocols; sites with several candidates stay `ambiguous` and never produce a finding. Every `SA_*`/`TX_*` rule therefore also applies to `self._session`-style attributes, and authentication reached through composed dependencies (`get_context -> get_current_user`) is recognised. The `interprocedural` block reports coverage and uncertainty (`references/audit-engine.md`).

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `ASYNC_TASK_REFERENCE_DROPPED` | `asyncio.create_task()`/`ensure_future()`/`loop.create_task()` is a bare statement: nothing keeps the task | That the task is ever collected in practice (it is allowed to be, per the Python docs) | Drop the reference, force `gc.collect()` during an await, assert the effect still happens; keep tasks in a set or a `TaskGroup` |
| `RETRY_UNBOUNDED` | A `tenacity`/`retrying`/`backoff` decorator without its stop/max argument (the libraries default to forever), or a `while True` loop that exits on success and retries on every exception with no attempt counter | That the dependency ever fails persistently | Point it at a dead endpoint and assert it gives up within the budget |
| `REL_HTTP_CALL_WITHOUT_TIMEOUT` | `requests.*` without `timeout=` (no default), `urlopen` without timeout, `httpx.Client/AsyncClient(timeout=None)`, or a verb on a `requests.Session` receiver without `timeout=` | That the server ever stalls; httpx clients with their default timeout are not reported | A server that accepts and never answers; assert the call fails in time |
| `REL_DUAL_WRITE_WITHOUT_OUTBOX` | One function commits and publishes to a broker/queue/producer that is external or reaches the network, with no outbox/inbox in sight | That the two writes are not already reconciled elsewhere; project-owned queue tables and in-process dispatchers are not reported | Kill the process between commit and publish; assert delivery exactly once after restart |
| `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` | An authenticated route takes a tenant id from the request and passes it to project calls while no dependency in the chain takes a tenant parameter and the principal never flows alongside (`evidence: unknown`) | That membership is not enforced elsewhere (middleware, RLS keyed on the principal, the service) | Member of A requests B's resources: 403/404 and no row |
| `SEC_PRIVILEGE_CHANGE_UNVERIFIED` | A route assigns `role`/`permissions`/`is_admin`-like attributes or calls `grant_access`/`revoke_role`/`promote_to_admin`-like functions, and only a generic authentication signal is visible (`evidence: unknown`) | That the caller is not already restricted by a policy the tool cannot name | A plain member attempts the change: 403 and no change |
| `ANALYSIS_PARTIAL` | A rule module or the summary pass hit a resource limit (recursion/memory) on this file; the rest ran | Nothing about the code | Simplify the pathological expression or accept the partial result |
| `TX_COMMIT_WITHOUT_ROLLBACK` (tightened) | Only handlers that can catch a commit failure count: bare `except`, `Exception`, `BaseException`, SQLAlchemy/DBAPI error families; handlers for custom domain exceptions are no longer reported | — | — |
| `RETRY_NON_IDEMPOTENT_WRITE` (tightened) | For hand-written loops only writes inside the retried `try` block count, and the loop must have a retry shape (a direct `try` that exits on success or whose handler goes around again); a write on the give-up path, a token refresh in the handler, or per-item error handling nested in an inner loop is not a retried write | That a retried read never has side effects on the provider | Same fault-injection test |
| `TX_COMMIT_IN_LOOP` (tightened) | `return` inside the `try` counts as a retry's success exit; a commit under an explicit `if index % BATCH == 0` guard is deliberate batching and is not reported | — | — |
| `SEC_*` through dependency factories | `Depends(require(p.X))`, `Depends(RoleChecker("admin"))` and aliases built on them are recognised; the factory's name and its arguments are the policy signal | That the factory enforces what its name says | A negative test per permission |
| `SA_SESSION_SHARED_CONCURRENTLY` (extended) | Also `gather(self._a(), self._b())` where both methods use the same `self._session`; the `call_path` names the methods | That the driver raises: asyncpg serialises statements with a lock, so the sharing is silent and the two units share one transaction (see `examples/tenant-ledger/tests/test_adversarial.py`) | Two units of work on one session where one rolls back: the other's write disappears |
| `TX_EXTERNAL_CALL_IN_TRANSACTION` (extended) | Also a project call inside the transaction block whose callee reaches the network within two calls; the `call_path` is the evidence | — | — |

## Rules added in v1.7 (schema 1.5, built on route contracts)

| Rule | Proves | Does not prove | Verify |
|---|---|---|---|
| `SEC_RESOURCE_AUTHZ_UNVERIFIED` | An authenticated route acts on a resource id from the path and hands it to project code without the principal, and no authorization kind is visible on the route (`evidence: unknown`) | That ownership is not enforced through ambient state (a ContextVar, middleware) or inside the service | A user unrelated to the resource calls the route: 403/404 and no change |
| `SEC_RLS_CONTEXT_FROM_REQUEST` | The row-level-security context (`set_config` / `SET LOCAL`, directly or through a project call) is set from a request-supplied tenant id in an authenticated route that does not bind that tenant to the principal (`evidence: verified`); it replaces the generic tenant-binding question for that route | That the leak is reachable in deployment (a gateway may bind the tenant) | Member of A asks for B: the reference implementation reproduces the leak on PostgreSQL and the membership binding that closes it |
| `ASYNC_COROUTINE_NOT_AWAITED` | A project `async def` is called and its coroutine is dropped: bare statement, assignment never awaited or scheduled, or `return coro()` from an `async def` | That the dropped work mattered | `-W error::RuntimeWarning` in tests, or assert the callee's side effect |
| `ASYNC_LOCK_PER_CALL` | A lock/semaphore is constructed inside the function that acquires it | That the critical section needed a lock at all (a database invariant needs a database guarantee) | Two concurrent calls sleeping inside the section are both inside |
| `REL_HTTP_CLIENT_PER_CALL` | An httpx client is built inside a function, used there and neither managed with `with`, closed, returned nor stored | That connections leak in a given runtime | Socket count under load; share the client in the lifespan |

Contracts and declarations: `public_routes` (globs) and `tenant_params` (domain tenant names) in `.audit-fastapi.json`; `# audit-fastapi: public` on a route. `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` stays silent on declared-public routes and no longer treats `@router.get("")` on a prefixed router as public. The words `staff` and `manager` are no longer privilege signals (they name user types); `is_staff`-style attribute changes are still privilege changes.

## Backlog (not implemented on purpose)

`SET LOCAL` outside an explicit transaction, hard-coded permissions, external identifiers, multiple commits per use case, long transactions by statement count, inbox/dedup detection on consumers, lock ordering and deadlock analysis, cancellation landing inside a driver operation, attribute chains deeper than one level, decorators that replace functions, Protocols with several implementations (deliberately `ambiguous`), authorization carried by ambient state (ContextVars, request.state), identity propagation into workers beyond the first hop, SQL filters whose tenant value comes from the request inside services. Each needs either deeper flow analysis or runtime data; they are documented here so their absence is not read as a clean bill.
