# The auditor's interprocedural engine: what it resolves, what it infers, what it cannot know

Read this when a report carries `evidence: inferred`, a `call_path`, or an `interprocedural` block you need to interpret, when deciding whether to trust a cross-function finding, or when extending the engine. The rule catalogue is `references/audit-rules.md`; the audit workflow is `references/architecture-audit.md`.

## Two passes, one principle

The auditor never executes, imports or type-checks the inspected code. Pass one reads every file for module-level facts (factories, `Annotated` aliases, routers, import-linter, layers, imports). Pass two builds a **summary per function** and links them into a **call graph**. Every link is syntactic: if the target cannot be named with certainty from imports and local bindings, the call is counted as unresolved and nothing is concluded from it. Conservative here means "silent when unsure", and the report says how much was unsure.

### Function summaries (`summaries.py`)
Per function: parameters and their declared kinds (`Session`, `AsyncSession`, HTTP clients), the calls it makes and how names flow into their arguments, whether it blocks (known blocking callables, sync `Session` I/O), writes (session commit/add/flush, HTTP POST/PUT/PATCH/DELETE), creates a session, sets a tenant context, is a route, has a `BackgroundTasks` parameter, carries a retry decorator or an attempt loop, shows an idempotency signal, and what a dependency returns or yields. Summaries hold no AST nodes, so a project with thousands of functions fits comfortably in memory.

### Call graph (`callgraph.py`)
A call site becomes an edge only when its target is one of:

| Shape | Resolution |
|---|---|
| `helper(x)` with `from app.svc import helper` | the imported function, including `app.x == src.app.x` aliases |
| `helper(x)` defined in the same module | the module-level function |
| `self.method()` inside a class | the method, through declared bases if inherited |
| `repo.method()` where `repo = ProjectClass(...)` or `repo: ProjectClass` | the class method (constructor and annotation instances only) |
| `Depends(f)` on a parameter | `f` as the provider of the parameter's kind |

Everything else is classified and counted: `external` (library functions), `dynamic` (attributes of unknown objects, `self.attr.method()`, callbacks), `not_found` (a project path with no function behind it, usually an attribute of a module-level object such as `router.get`), `constructors`. The counts appear in `interprocedural.resolution`; a project where most sites are `dynamic` gets little from the pass, and the report shows it rather than guessing.

### Kinds flow (`kinds.py`, propagation)
A parameter's kind is taken, in this order of trust, from: its annotation (including `Annotated[...]` aliases defined anywhere in the project, `async_sessionmaker[...]` factory annotations, constructors and module-level factories) → **verified**; the return or yield type of its `Depends` provider → **verified** (the dependency's own AST is the evidence); the kind of the argument a caller passes → **inferred**. Propagation runs to a fixed point bounded by the call depth. Declared kinds always win over inferred ones, so a wrong caller never overrides an annotation.

## Evidence and confidence

| `evidence` | Meaning | Effect |
|---|---|---|
| `verified` | the pattern and the symbols involved were resolved directly from the AST | confidence as defined by the rule |
| `inferred` | a type or flow was propagated from a caller or guessed from a name (`session`, `db`) | confidence lowered one level; the message says what was inferred |
| `unknown` | the property could not be resolved; the finding is a question | low confidence, `verification: not_verifiable_statically` |

A `call_path` lists the chain the engine followed, from the caller to the symbol: `src.app.routes.report -> src.app.helpers.compute_report -> src.app.helpers.slow_total -> time.sleep`. Each step is a resolved edge; the path is reproducible by reading those functions in order.

Nothing inferred is ever reported as a confirmed defect or a vulnerability. `verification` keeps saying whether a runtime test is needed; `evidence` says how the static claim was obtained.

## Limits (all explicit)

| Limit | Default | Where it shows |
|---|---|---|
| Call depth | 4 project functions after the caller (`--max-call-depth`) | `interprocedural.max_depth`; a shorter depth hides deeper blocking paths |
| Time budget | 5 s for summaries plus graph | `truncated: true` with `truncated_reason`; the report is still produced |
| Propagation rounds | depth + 1 | `propagation_rounds` |
| Recursion and cycles | visited sets on every walk | cycles never loop; a recursive helper is followed once |
| Dynamic resolution | none | `resolution.dynamic`; decorators that wrap functions are not interpreted |
| Imports not found | counted, not guessed | `resolution.not_found` |
| Memory | summaries only, AST discarded per file | large projects scale with function count, not file size |

`--no-interprocedural` disables the pass; the report then equals v1.4 behaviour and says `interprocedural.enabled: false`.

## Rules that use the engine

- `ASYNC_BLOCKING_CALL_INDIRECT`: async function → sync project helper → blocking call, within the depth. Not reported in tests (fixtures call sync helpers freely); one finding per helper per file.
- `SA_ASYNC_SESSION_NOT_AWAITED`, `SA_SYNC_SESSION_IN_ASYNC_DEF`, `TX_COMMIT_IN_LOOP`, `TX_COMMIT_WITHOUT_ROLLBACK`, `SA_SESSION_SHARED_CONCURRENTLY`: unchanged logic, now also on parameters typed through dependencies and callers, with `evidence` set accordingly.
- `RETRY_NON_IDEMPOTENT_WRITE`: retry decorator or attempt loop + a write reachable within two calls + no idempotency signal.
- `BG_REQUEST_SCOPED_RESOURCE`: a dependency-provided session handed to `add_task`/`create_task` from a route.
- `BG_TASK_CONTEXT_UNVERIFIED`: the task target writes, receives no principal/tenant/session context and does not recreate one (own session, tenant setting). Reported only when the spawning function holds request context; classified as captured / recreated / unverifiable.
- `SA_ASYNC_LAZY_LOAD_RISK`: relationship registry from pass one + loaded-object tracking in pass two.

## Known false positives and negatives

False positives to expect: a sync helper called from `async def` but always wrapped by the caller in `to_thread` at a level the engine does not see; a retry decorator around a write that is idempotent by database design without a visible key or `ON CONFLICT`; a session passed to `create_task` that the handler awaits immediately (sequential in practice).

False negatives to expect: calls through object attributes (`self.repo.save`), callbacks and framework dispatch; sessions injected without annotation and without a resolvable dependency; writes performed through libraries the summaries do not recognise; context recreated through helpers deeper than two calls; anything behind `getattr`, decorators that replace functions, or dynamic imports.

When a finding matters, the fixture projects under `tests/fixtures/interproc` and the reference implementation `examples/tenant-ledger` show both the detection and the runtime test that closes it.
