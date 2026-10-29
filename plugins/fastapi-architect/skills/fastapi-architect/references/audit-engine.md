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

## Rules that use the engine

- `ASYNC_BLOCKING_CALL_INDIRECT`: async function → sync project helper → blocking call, within the depth; through attributes, returns, callbacks and single-implementation interfaces too (then `inferred`). Not reported in tests; one finding per helper per file.
- `SA_ASYNC_SESSION_NOT_AWAITED`, `SA_SYNC_SESSION_IN_ASYNC_DEF`, `TX_COMMIT_IN_LOOP`, `TX_COMMIT_WITHOUT_ROLLBACK`, `TX_EXTERNAL_CALL_IN_TRANSACTION`, `SA_SESSION_SHARED_CONCURRENTLY`: unchanged logic, now also on `self.<attr>` sessions and clients, on parameters typed through dependencies and callers, and (for the transaction rule) through project calls that reach the network within two calls, with `evidence` set accordingly.
- `SA_SESSION_SHARED_CONCURRENTLY` through methods: `gather(self._debit(), self._credit())` where both methods use `self._session`; the `call_path` names the methods and the attribute.
- `RETRY_NON_IDEMPOTENT_WRITE`: retry decorator or attempt loop + a write reachable within two calls + no idempotency signal.
- `BG_REQUEST_SCOPED_RESOURCE`: a dependency-provided session (also typed through an alias) handed to `add_task`/`create_task` from a route or a handler-shaped function.
- `BG_TASK_CONTEXT_UNVERIFIED`: the task target writes, receives no principal/tenant/session context and does not recreate one. Reported only when the spawning function holds request context; classified as captured / recreated / unverifiable.
- `SA_ASYNC_LAZY_LOAD_RISK`: relationship registry from pass one + loaded-object tracking in pass two.
- `SEC_ROUTE_WITHOUT_AUTH_SIGNAL`, `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL`, `SEC_PRIVILEGE_CHANGE_UNVERIFIED`: follow `Depends` chains three levels deep, so authentication reached through `get_context -> get_current_user` is recognised and the absence of a function named `authorize` is never the evidence.
- `REL_DUAL_WRITE_WITHOUT_OUTBOX`: a publish that resolves to project code is a dual write only when that code reaches the network; a project-owned queue table or an in-process dispatcher is not.

## Known false positives and negatives

False positives to expect: a sync helper called from `async def` but always wrapped by the caller in `to_thread` at a level the engine does not see; a retry decorator around a write that is idempotent by database design without a visible key or `ON CONFLICT`; a session passed to `create_task` that the handler awaits immediately (sequential in practice); a single-implementation Protocol whose production binding is a class the project does not contain (then the edge is `inferred` and says so); a tenant binding enforced by middleware or by row-level security keyed on the principal (`SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` is a question with `evidence: unknown`).

False negatives to expect: calls through attributes of unknown origin (`self.repo` assigned outside `__init__` from an untyped value), attribute chains deeper than one level (`self.a.b.c()`), callbacks the framework supplies, `getattr`, decorators that replace functions, dynamic imports, sessions injected without annotation and without a resolvable dependency, writes performed through libraries the summaries do not recognise, context recreated through helpers deeper than two calls, Protocols with several implementations (deliberately `ambiguous`).

When a finding matters, the ground-truth corpus under `tests/fixtures/truth16`, the fixture projects under `tests/fixtures/interproc` and the reference implementation `examples/tenant-ledger` show both the detection and the runtime test that closes it. `scripts/audit_precision.py` scores precision, recall, coverage, time and memory against that corpus and against a previous commit.
