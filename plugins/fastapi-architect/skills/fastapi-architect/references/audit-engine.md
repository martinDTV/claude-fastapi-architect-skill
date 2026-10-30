# The auditor's interprocedural engine: what it resolves, what it infers, what it cannot know

Read this when a report carries `evidence: inferred`, a `call_path`, an `interprocedural` block you need to interpret (coverage, ambiguous sites, truncation), when deciding whether to trust a cross-function finding, or when extending the engine. The rule catalogue is `references/audit-rules.md`; the audit workflow is `references/architecture-audit.md`.

## Two passes, one principle

The auditor never executes, imports or type-checks the inspected code. Pass one reads every file for module-level facts (factories, `Annotated` aliases, routers, import-linter, layers, imports, re-exports, module-level objects and instances). Pass two builds a **summary per function** and links them into a **call graph**. Every link is syntactic: if the target cannot be named with certainty from imports and local bindings, the call is classified and counted, and nothing is concluded from it. Conservative here means "silent when unsure", and the report says how much was unsure.

### Function summaries (`summaries.py`)
Per function: parameters and their declared kinds (`Session`, `AsyncSession`, HTTP clients, including types reached through project facades and `Annotated` aliases), the calls it makes and how names flow into their arguments, which functions it hands to other functions as arguments, whether it blocks (known blocking callables, sync `Session` I/O), writes (session commit/add/flush, HTTP POST/PUT/PATCH/DELETE), makes network calls, creates a session, sets a tenant context, is a route, has a `BackgroundTasks` parameter, carries a retry decorator or an attempt loop, shows an idempotency signal, what a dependency returns or yields, which project class a function returns, and which `self.<attr>` receivers a method touches. Per class: methods, declared bases, Protocol/abstract markers, and the attributes assigned in `__init__` with their visible type (`self._session = session` where `session: AsyncSession`; `self._repo = Repo(...)`; `x: T` class annotations). Summaries hold no AST nodes, so a project with thousands of functions fits comfortably in memory.

### Call graph (`callgraph.py`)
A call site becomes an edge only when its target is one of:

| Shape | Resolution | Edge kind / evidence |
|---|---|---|
| `helper(x)` with `from app.svc import helper`, `from .svc import helper` or `from . import svc` | the imported function, through re-exports (`app/__init__.py`, `public.py`) and `app.x == src.app.x` aliases | direct / verified |
| `helper(x)` defined in the same module | the module-level function (a parameter or local of the same name shadows it) | direct / verified |
| `self.method()` inside a class | the method, through declared bases if inherited | direct / verified |
| `repo.method()` where `repo = ProjectClass(...)`, `repo: ProjectClass`, or `repo` is a module-level instance of a project class | the class method; a Protocol/abstract class resolves to its implementation only when exactly one exists | direct or interface / verified or inferred |
| `self._repo.method()` where `__init__` assigned a typed parameter or a constructor to `self._repo` | the attribute's class method | attribute / verified (annotated) or inferred (kind arrived through a constructor call) |
| `obj.method()` where `obj = make_obj(...)` and `make_obj` has an annotated return type naming a project class | the class method | return / verified |
| `handler(x)` where `handler` is a parameter | the function every caller hands in, when all callers agree | callback / inferred |
| `Cls.method()`, `Cls.build()` (static and class methods) | the method | direct / verified |
| `Depends(f)` on a parameter, including through an `Annotated` alias | `f` as the provider of the parameter's kind | — |

Everything else is classified and counted: `external` (library functions and methods on receivers whose type is a library type or a known session/client kind), `builtin` (`len`, `str`, methods of `str`/`dict`/`list` values and of builtin-typed parameters), `object` (module-level objects such as routers, factories, context variables and constants), `constructors` (project classes without an `__init__` of their own), `ambiguous` (several candidates with no evidence to choose: a Protocol with several implementations, a callback parameter that receives different functions), `not_found` (a project module exists but defines no such symbol) and `dynamic` (attributes of unknown objects, callbacks the framework supplies, loop variables, deep attribute chains). A library sharing a directory name with the project (`alembic/`, `tests/`) is external, not `not_found`.

### Kinds flow (`kinds.py`, propagation)
A parameter's kind is taken, in this order of trust, from: its annotation (including `Annotated[...]` aliases defined anywhere in the project, `async_sessionmaker[...]` factory annotations, constructors, module-level factories and types re-exported through facades) → **verified**; the return or yield type of its `Depends` provider → **verified**; the kind of the argument a caller passes → **inferred**. Instance attributes take their kind from the `__init__` parameter they copy (verified when annotated, inferred when the kind reached the constructor through a caller). Propagation runs to a fixed point bounded by the call depth; functions handed as arguments flow the same way, so a helper that only forwards its callback still resolves. Declared kinds always win over inferred ones, so a wrong caller never overrides an annotation.

## Evidence and confidence

| `evidence` | Meaning | Effect |
|---|---|---|
| `verified` | the pattern and the symbols involved were resolved directly from the AST | confidence as defined by the rule |
| `inferred` | a type or flow was propagated from a caller or guessed from a name, or one step of the call path went through an interface, a callback or a caller-typed value | confidence lowered one level; the message says what was inferred |
| `unknown` | the property could not be resolved; the finding is a question | low confidence, `verification: not_verifiable_statically` |

A `call_path` lists the chain the engine followed, from the caller to the symbol: `app.routes.report -> app.helpers.compute_report -> app.helpers.slow_total -> time.sleep`. Each step is a resolved edge; the path is reproducible by reading those functions in order. Ambiguous sites never produce a path: a finding is never built on a guess between candidates.

Nothing inferred is ever reported as a confirmed defect or a vulnerability. `verification` keeps saying whether a runtime test is needed; `evidence` says how the static claim was obtained.

## Reading the `interprocedural` block (schema 1.4)

| Field | Meaning |
|---|---|
| `resolution` | call sites by outcome: `project`, `external`, `builtin`, `object`, `constructors`, `ambiguous`, `not_found`, `dynamic`; the values sum to `call_sites` |
| `edges` | how each project edge was resolved: `direct`, `attribute`, `return`, `callback`, `interface` |
| `coverage.project_call_coverage` | `project / (project + not_found + ambiguous)`: of the sites that name a project symbol, how many resolved. This is the honest coverage; the raw edge count is not |
| `coverage.classified_ratio` | `(call_sites - dynamic) / call_sites`: how much of the code the engine could say anything about |
| `uncertainty` | `ambiguous_sites`, `dynamic_sites`, `callback_params_unresolved` (parameters called as functions whose callers never hand in a project function: framework callbacks such as ASGI `send`/`receive`), `interfaces` (Protocol/abstract classes seen) |
| `limits` | `max_depth`, `time_budget_s`, `max_base_walk`, `max_implementations`, `propagation_rounds_max` as configured for this run |
| `attribute_kinds` | instance attributes with a session/client kind |
| `files_skipped` | files whose summaries hit a resource limit (they also get an `ANALYSIS_PARTIAL` finding) |
| `truncated`, `truncated_reason` | whether the time budget cut the pass |

On two real repositories (read-only), `project_call_coverage` was 0.97 and `classified_ratio` 0.82–0.88; the remaining `dynamic` sites are loop variables, framework-supplied callbacks and attribute chains on values of unknown origin. A project where most sites are `dynamic` gets little from the pass, and the report shows it rather than guessing.

## Limits (all explicit)

| Limit | Default | Where it shows |
|---|---|---|
| Call depth | 4 project functions after the caller (`--max-call-depth`) | `limits.max_depth`; a shorter depth hides deeper blocking paths |
| Time budget | 5 s for summaries plus graph (`--time-budget`) | `truncated: true` with `truncated_reason`; the report is still produced and `partial_analysis` is true |
| Propagation rounds | depth + 1 | `propagation_rounds` |
| Implementations per interface | 16 examined; more becomes `ambiguous` | `limits.max_implementations` |
| Recursion and cycles | visited sets on every walk | cycles never loop; a recursive helper is followed once |
| Dynamic resolution | none | `resolution.dynamic`; decorators that replace functions are not interpreted |
| Imports not found | counted, not guessed | `resolution.not_found` |
| Pathological files | parser limits (`FILE_SKIPPED`, `PY_SYNTAX`), per-rule-module recursion guard (`ANALYSIS_PARTIAL`), summary skip (`files_skipped`) | the rest of the project is analysed normally |
| Memory | summaries only, AST discarded per file | large projects scale with function count, not file size |

Determinism: for the same input the report is byte-identical across runs, including the `interprocedural` block. The one exception is a wall-clock truncation: what a budget cuts depends on the machine, so a truncated report says so and should be rerun with a larger `--time-budget` before being compared with a baseline. `--no-interprocedural` disables the pass; the report then equals v1.4 behaviour for cross-function rules and says `interprocedural.enabled: false` (the v1.6 per-function reliability checks still run).

## Security contracts (schema 1.5)

Since v1.7 the report carries a `contracts` block: one entry per route with what the source shows, classified and never judged. `authentication` is `present` when a `Depends`/`Security` chain (up to three levels, through `Annotated` aliases, permission factories such as `require(perm)`, class-based checkers such as `RoleChecker("admin")`, facade re-exports), the router's or the application's `dependencies`, or an authentication middleware is visible; `absent` otherwise. `authorization` lists the kinds visible on the route: `role` (a checker, factory or dependency whose name or arguments name a role/permission/policy), `scopes` (`Security(..., scopes=[...])`), `decorator` (`@requires("admin")`-style), `membership` (a dependency that receives the tenant id with the principal), `resource_dependency` (a dependency that receives the route's resource id), `principal_with_resource` (the principal flows into the same project call as the resource or tenant id), `authorization_call` (a call to an authorization service with the principal), `tenant_comparison` (the tenant id compared with the principal's data), `router_dependencies`. `public` is true only when declared: a health-style path, a `public_routes` glob in `.audit-fastapi.json`, or `# audit-fastapi: public` on the route; a collection endpoint (`@router.get("")`) on a prefixed router is not public. `principal_params`, `tenant_params` (built-in names plus `tenant_params` from the configuration, for domain names such as `clinic_id`) and `resource_params` (path ids) complete the picture; `evidence` is `unknown` when authentication is present, no authorization kind is visible and the route acts on a tenant or resource id.

The `partition` puts every route in exactly one bucket with independent definitions: `public_declared` (declared or health-style path), `unauthenticated_not_public` (no authentication signal and no declaration), `optional_authentication` (the dependency admits anonymous callers: `optional_user`, `Driver | None`), `authenticated_authorization_visible` (at least one authorization kind), `authenticated_only` (authenticated, no kind visible, and the route acts on no resource or tenant id, so there is nothing to relate), `authenticated_authorization_unknown` (authenticated, no kind visible, and it acts on a resource or tenant id). The older `summary` counters are kept; `authorization_unknown` there equals the last bucket. The partition is the number to watch in CI: a route that becomes public or loses its visible authorization shows up there before any rule fires. The rules built on contracts (`SEC_ROUTE_WITHOUT_AUTH_SIGNAL`, `SEC_RESOURCE_AUTHZ_UNVERIFIED`, `SEC_RLS_CONTEXT_FROM_REQUEST`, `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL`, `SEC_PRIVILEGE_CHANGE_UNVERIFIED`) only ask questions where a kind is missing; a function named `authorize` is never required and its absence is never the evidence.

## Evidence states (schema 1.5)

Every finding carries one `evidence_state`, derived after classification so suppression wins: `suppressed` (a documented `# noqa`/`# audit-fastapi: ignore` marker; kept for review), `partial` (`ANALYSIS_PARTIAL`, `FILE_SKIPPED`, `PY_SYNTAX`: the analysis of that file did not complete), `insufficient` (`evidence: unknown` or `verification: not_verifiable_statically`: a question), `inferred` (a propagated type or flow; confirm the target), `static_confirmed` (pattern and symbols resolved from the source; impact may still need a runtime check, which `verification` says). `reproduced` exists in the enumeration for reports that record a runtime reproduction (the reference implementation's tests are such records) and is never produced by the static tool: a semantic pattern is not a proof of exploitation. `summary.by_evidence_state` counts them and the SARIF export carries the state per result.

## Declared contracts (opt-in)

`.audit-fastapi.json` may declare a `contracts` object: `route_auth_required` (globs on the full route path: those routes must be authenticated or declared public), `mutation_authorization_required` (globs: POST/PUT/PATCH/DELETE there must show an authorization kind), `forbidden_imports` (pairs `{"from": "app.api", "to": "app.infra"}` on module prefixes), `commit_layers` (layer names that own transactions; layers come from `layers` in the same file or the default path patterns). The declarations are plain JSON read with `json.load`; no project Python is imported or executed, and malformed values are ignored. The four `CONTRACT_*` rules (category `contracts`) report "the code disagrees with what the project wrote down", never an opinion about what the architecture should be.

## SARIF 2.1.0

`--format sarif` prints and `--sarif FILE` writes a SARIF 2.1.0 document (`$schema` points at the OASIS 2.1.0 errata01 schema) with one run, the rules actually used, one result per finding (level from severity: high → error, medium → warning, low/info → note; `%SRCROOT%` relative locations; the stable fingerprint as `partialFingerprints["fastapiArchitect/fingerprint/v1"]`; documented suppressions as `suppressions[].kind = "inSource"`; confidence, evidence, evidence_state, verification, call path and recommended test as result properties; `baselineState` when `--baseline` is given) and the interprocedural and contracts summaries as run properties. Text, JSON, markdown and `--output` are unchanged; the SARIF output is deterministic.

## Rules that use the engine

- `ASYNC_BLOCKING_CALL_INDIRECT`: async function → sync project helper → blocking call, within the depth; through attributes, returns, callbacks and single-implementation interfaces too (then `inferred`). Not reported in tests; one finding per helper per file.
- `SA_ASYNC_SESSION_NOT_AWAITED`, `SA_SYNC_SESSION_IN_ASYNC_DEF`, `TX_COMMIT_IN_LOOP`, `TX_COMMIT_WITHOUT_ROLLBACK`, `TX_EXTERNAL_CALL_IN_TRANSACTION`, `SA_SESSION_SHARED_CONCURRENTLY`: unchanged logic, now also on `self.<attr>` sessions and clients, on parameters typed through dependencies and callers, and (for the transaction rule) through project calls that reach the network within two calls, with `evidence` set accordingly.
- `SA_SESSION_SHARED_CONCURRENTLY` through methods: `gather(self._debit(), self._credit())` where both methods use `self._session`; the `call_path` names the methods and the attribute.
- `RETRY_NON_IDEMPOTENT_WRITE`: retry decorator or attempt loop + a write reachable within two calls + no idempotency signal.
- `BG_REQUEST_SCOPED_RESOURCE`: a dependency-provided session (also typed through an alias) handed to `add_task`/`create_task` from a route or a handler-shaped function.
- `BG_TASK_CONTEXT_UNVERIFIED`: the task target writes, receives no principal/tenant/session context and does not recreate one. Reported only when the spawning function holds request context; classified as captured / recreated / unverifiable.
- `SA_ASYNC_LAZY_LOAD_RISK`: relationship registry from pass one + loaded-object tracking in pass two.
- `SEC_ROUTE_WITHOUT_AUTH_SIGNAL`, `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL`, `SEC_PRIVILEGE_CHANGE_UNVERIFIED`, `SEC_RESOURCE_AUTHZ_UNVERIFIED`, `SEC_RLS_CONTEXT_FROM_REQUEST`: built on the route contracts; follow `Depends` chains three levels deep, so authentication reached through `get_context -> get_current_user` is recognised and the absence of a function named `authorize` is never the evidence.
- `ASYNC_COROUTINE_NOT_AWAITED`: the callee is a project `async def` resolved through the graph (also `self.method()` and instances); the coroutine is dropped when it is a bare statement, assigned to a name never awaited or scheduled, or returned from an `async def`.
- `REL_DUAL_WRITE_WITHOUT_OUTBOX`: a publish that resolves to project code is a dual write only when that code reaches the network; a project-owned queue table or an in-process dispatcher is not.

## Known false positives and negatives

False positives to expect: a sync helper called from `async def` but always wrapped by the caller in `to_thread` at a level the engine does not see; a retry decorator around a write that is idempotent by database design without a visible key or `ON CONFLICT`; a session passed to `create_task` that the handler awaits immediately (sequential in practice); a single-implementation Protocol whose production binding is a class the project does not contain (then the edge is `inferred` and says so); a tenant binding enforced by middleware or by row-level security keyed on the principal (`SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` is a question with `evidence: unknown`).

False negatives to expect: calls through attributes of unknown origin (`self.repo` assigned outside `__init__` from an untyped value), attribute chains deeper than one level (`self.a.b.c()`), callbacks the framework supplies, `getattr`, decorators that replace functions, dynamic imports, sessions injected without annotation and without a resolvable dependency, writes performed through libraries the summaries do not recognise, context recreated through helpers deeper than two calls, Protocols with several implementations (deliberately `ambiguous`).

When a finding matters, the ground-truth corpus under `tests/fixtures/truth16`, the fixture projects under `tests/fixtures/interproc` and the reference implementation `examples/tenant-ledger` show both the detection and the runtime test that closes it. `scripts/audit_precision.py` scores precision, recall, coverage, time and memory against that corpus and against a previous commit.
