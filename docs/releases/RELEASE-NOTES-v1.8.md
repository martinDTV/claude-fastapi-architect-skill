# FastAPI Architect v1.8 — Program Analysis Precision & Verified Remediation Foundations

Additive release on top of v1.7.0 (`e8224ce`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id, the suppression markers and the fingerprints. Report schema 1.6 is a superset of 1.0–1.5: it adds `details` and `remediation` per finding, `facts.commit_loops`, three coverage shares and four uncertainty keys in `interprocedural`, and a `changes` block (plus lists) in `comparison`; the v1.4 `comparison.counts` keeps exactly its three keys.

## Requirements of the brief

| Phase | Requirement | Status | Where |
|---|---|---|---|
| P1 | Call classification: local origins (plain, awaited, unpacked, `with`), external results, closures, comprehension variables, loop targets; no invented project edges; v1.7 metrics kept as baseline | complete | `summaries.py` (`_bind_origin`, `_call_origin_class`, `inherited_kinds`, `CallSite.origin`), `callgraph.py` (`dynamic_by_origin`, coverage shares) |
| P2 | Callbacks and uncertainty: ASGI `send`/`receive`/`app`, typed `Callable`, callback parameters, lambdas forwarding to one target, bound-method references; `callback_params_unresolved` corrected; separate metrics for classification, external calls and project coverage; hard cases itemised, not hidden | complete | `callback_kinds`, `asgi_signature`, `_lambda_target`, `framework_callbacks`, `callback_sites_unresolved`, `project_site_ratio` / `external_site_ratio` / `unresolved_site_ratio` |
| P3 | Attributes and mixins, conservatively: `self.a.b.method()`, declared attribute types, mixins, inherited methods, external bases; ambiguity never a confirmed finding; negative controls | complete | `attr2`/`iattr` hints, `mixin_attribute_kinds`, `_subclass_methods`, `_has_external_base`; negatives in `tests/test_audit_v18.py` and both corpora |
| P4 | Transactional precision: per-item vs deliberate batch, pagination, retry, convergence, incorrectly shared transaction; structural evidence only; fingerprints kept; intentional changes documented | complete | `classify_commit_loop`, `details.loop_kind`, `facts.commit_loops`, `TX_COMMIT_INSIDE_BEGIN_BLOCK` (verified on PostgreSQL) |
| P5 | Verifiable remediation foundations: problem, evidence, certainty, proposed fix, invariants, tests, regression risk; never modifies code; never claims verified | complete as foundations (proposals only; no application, no verification) | `fastapi_audit/remediation.py`, `finding.remediation` for five rules |
| P6 | Before/after comparison: new, resolved, persistent, severity and confidence changes, partial results, suppressions; fingerprints preserved; "no longer detected", not "resolved" | complete | `report.compare_reports`, `scripts/audit_diff.py` (skill + root shim), `--baseline` |
| P7 | Independent tests: 251 tests and 58 rules kept; fixtures before detectors; dev and acceptance corpora separate; negative and adversarial tests | complete: 292 tests (41 new), 59 rules | `tests/test_audit_v18.py`, `tests/fixtures/truth18/{dev,acceptance}` |
| P8 | Calibration on Clivana and AgentForge, read-only: coverage, classification, uncertainty, false positives, known negatives, time, memory, preserved/new/disappeared findings, justified | complete | this document |
| P9 | Compatibility: CLI, JSON 1.0–1.5, SARIF, declarative contracts, fail-closed configuration, rule ids, fingerprints, suppressions, tenant-ledger, 47 evals, references | complete | `CompatibilityTests` in v1.4–v1.8 suites; SARIF validated against the official schema; 47 eval cases unchanged |
| P10 | Validation: unittest on 3.9 and 3.13, pytest, PostgreSQL, SARIF schema, configuration validation, determinism, benchmarks, structure, `claude plugin validate`, secrets scan, `git diff --check`; no paid evals | complete | this document |
| P11 | Thematic local commits, martinDTV identity, no push, no tags, no history rewrite, no trailers | complete (local only) | `git log e8224ce..HEAD` |

## Baseline (Phase 0)

Git at `e8224ce`, version 1.7.0, 251 tests green, 58 rules, schema 1.5, 47 evaluation cases, `tenant-ledger` at 29 tests. Calibration repositories (read-only): Clivana 55 findings, AgentForge 11. Reports of the v1.7 engine were frozen before any change and every v1.8 comparison below runs the v1.7 engine from `git archive e8224ce` in a separate process.

The first v1.8 corpus (`tests/fixtures/truth18/dev`, 7 files, 37 markers) was written before the engine work. The v1.7 engine on it, as first measured: true positives 5, false positives 1, false negatives 6, precision 0.833, recall 0.455, F1 0.588, expected edges 1/6, expected unresolved statuses 7/16. After the loop decision recorded below (deliberate loop kinds expected absent instead of reported) the same engine scores F1 0.308 (2 TP, 4 FP, 5 FN), because its `len(buffer) >= N`, paginated and convergence reports count as false positives; both numbers are kept here.

## Independent corpora (Phase 7)

- `dev` (7 files, 37 markers; P1–P4 cases with negative controls): scored in-process by `tests/test_audit_v18.py`. One change after the first write, recorded in its change log: the `batch`, `paginated` and `convergence` loop-commit markers were flipped from "reported with lower confidence" to **absent**, before the first scoring of the v1.8 engine, for the reasons given under P4 below.
- `acceptance` (6 files, 23 markers; a CRM domain with different shapes): written by the same author as the engine, so it is an acceptance corpus, not a blind holdout. It was displayed once during the implementation (to confirm the loop shapes it contains) and edited once before any scoring (the same three loop markers), both recorded in its `EXPECTATIONS.md`. It was scored **twice**, both results kept:

| Corpus | v1.7.0 | v1.8.0 | Notes |
|---|---|---|---|
| dev, precision / recall / F1 | 0.333 / 0.286 / 0.308 | 1.0 / 1.0 / 1.0 | FPR 0.444 → 0.0; FNR 0.714 → 0.0; expected edges 1/6 → 6/6; unresolved statuses kept 7/16 → 16/16; project-call coverage 0.812 → 0.905; classified 0.76 → 0.917 |
| acceptance, first scoring | 0.25 / 0.2 / 0.222 | 1.0 / 0.8 / 0.889 | edges 1/4 → 4/4; unresolved 5/9 → 8/9. Two misses: a synchronous closure inside a method using `self._session` (the closure had no class context) and a `send_wrapper` closure awaiting the outer ASGI `send` (callback kinds were not inherited by closures) |
| acceptance, second scoring | same | 1.0 / 1.0 / 1.0 | after two engine fixes prompted by the first scoring (closures read the enclosing method's `self`; closures inherit callback kinds), each covered by a regression test; unresolved 9/9; classified 0.841 → 0.952 |

`truth16` and the three `truth17` partitions are unchanged and still pass in the suite (`truth17/holdout` was not re-scored).

## What changed in the engine (P1–P3)

Every case below is classified without executing anything and without inventing a project edge; ambiguity stays `ambiguous` or unknown and never produces a finding. Details and the per-case evidence class are in `references/audit-engine.md`.

- Locals bound to library or builtin call results (plain, awaited, unpacked pairwise or from one call, `with ... as`), project callees whose return annotation is builtin (`-> str`, `-> list[dict]`) or a library type, closures reading the enclosing function's declared kinds, instances and origins, `self.<attr>` values whose attribute is a dict literal or a typed client.
- ASGI callbacks (`Send`, `Receive`, `ASGIApp` from starlette/asgiref, or the untyped `(scope, receive, send)` triple) are `external` and counted in `framework_callbacks`; `message = await receive()` makes `message.get()` external. Typed `Callable[...]` parameters (v1.7 saw no type for generics) are callback hints; lambdas that only forward their parameters, bound-method references on typed project instances and `self.method` feed callback resolution; two providers stay `ambiguous`.
- `self.a.b.method()` through two typed links, same-module class names in constructor and parameter annotations (`uow: UnitOfWork`, `worker: Worker`), mixins borrowing an attribute kind when every concrete subclass agrees (one disagreeing or untyped subclass cancels it), template-method hooks resolving to the unique subclass implementation (`inferred`; several stay `ambiguous`), and unknown methods of classes with a third-party, stdlib or builtin base (`TypedDict`, `BaseModel`, `Exception`) classified `external` instead of `not_found`; `Protocol`, `Generic`, `ABC` and `object` are not treated as method providers.
- Every still-dynamic site carries an origin (`uncertainty.dynamic_by_origin`): `loop_target`, `comprehension`, `untyped_param`, `unpack`, `closure_unknown`, `callback_result`, `call_result_unknown`, `local_unknown`, `attribute_chain`, `attribute_untyped`, `return_untyped`, `callback_unresolved`, `unresolved_name`, `nested_function`, `expression`. Coverage is reported as three shares that sum to one. `callback_params_unresolved` now counts distinct parameters without a provider and excludes framework callbacks (v1.7 counted sites and included ASGI `send`/`receive`); `callback_sites_unresolved` keeps the site count.
- Security contracts: an `Annotated[..., Depends(f)]` alias assigned inside an enclosing function (application factories) is recognised, so `_: Authenticated` authenticates and `_: Admin` built on `admin_authenticated` is role authorization; the resource question no longer prints an empty principal label.

### Classification on the calibration repositories (read-only, v1.7 → v1.8)

| Repository | Call sites | project | external | builtin | dynamic | not_found | ambiguous | attribute edges | interface edges | classified ratio |
|---|---|---|---|---|---|---|---|---|---|---|
| Clivana | 27,310 | 9,778 → 9,790 | 9,270 → 9,777 | 2,784 → 2,953 | 3,263 → 2,661 (−18 %) | 242 → 152 | 18 → 22 | 547 → 550 | 0 → 0 | 0.881 → 0.902 |
| AgentForge | 14,996 | 4,771 → 4,954 | 3,710 → 3,886 | 2,669 → 2,724 | 2,678 → 2,342 (−13 %) | 98 → 4 | 57 → 73 | 88 → 198 | 4 → 10 | 0.821 → 0.844 |
| tenant-ledger | 776 | 196 → 196 | 326 → 327 | 40 → 40 | 177 → 176 | 4 → 4 | 0 → 0 | 0 → 0 | 0 → 0 | 0.772 → 0.773 |

Per-site diff between the two engines: 0 project edges retargeted, 0 project edges lost, 0 classified sites that became dynamic, on all three. Moves on Clivana: dynamic → external 421, dynamic → builtin 160, not_found → external 90, dynamic → project 12, external → builtin 9, dynamic → ambiguous 4. On AgentForge: dynamic → project 182, not_found → external 94, dynamic → external 84, dynamic → builtin 49, dynamic → ambiguous 16, external → builtin 5. Remaining origins on Clivana: `expression` 910, `local_unknown` 656, `untyped_param` 540, `comprehension` 107, `closure_unknown` 103, `return_untyped` 75, `unresolved_name` 67, `attribute_chain` 61, `attribute_untyped` 54, `loop_target` 46, `unpack` 19, `callback_unresolved` 17, `call_result_unknown` 10; project shares 0.359 / 0.538 / 0.104 (project / external / unresolved).

## Transactional precision (P4)

`TX_COMMIT_IN_LOOP` reads the loop's structure before reporting: `per_item` (reported, as before, with `details.loop_kind`, `loop_evidence`, `loop_line`; message and fingerprint unchanged), `batch` (commit guarded by a modulo test, by `len(buffer) >= N`, by a counter incremented in the loop, or a loop over `chunked(...)`/`range(a, b, step)`), `paginated` (a `while`/`range` loop that stops when a fetched page is empty), `convergence` (a `while`/`range` loop that stops when a non-emptiness condition holds) and `retry` (unchanged since v1.6). Deliberate kinds are counted in `facts.commit_loops` and **not reported**. Names prove nothing: a `BATCH_SIZE` constant next to a per-item commit changes nothing (tested).

Intentional change, documented here and in `references/audit-rules.md`: v1.7 silenced the modulo form only and reported `len(buffer) >= N` batches, paginated and convergence loops as per-item commits. v1.8 treats every structurally deliberate kind the same way, because the rule's own text names deliberate batching as the acceptable outcome, the modulo decision was already reviewed and approved at the v1.6 calibration, and re-reporting it would surface new findings in every existing baseline (`--fail-on-new`). On Clivana this removed three findings, each inspected in the source: `CalendarSyncService.mirror_appointment` (a bounded `for _ in range(MIRROR_PASSES)` that commits each pass and stops when the mirrored appointment is stable: convergence), `ClinicalRecordService.reseal_organization` (a cursor-paginated `while covered < limit` resealing job: paginated) and `prune_expired` (`for _ in range(PRUNE_MAX_BATCHES)` deleting in batches and stopping on a short batch: labelled convergence by the classifier, deliberate either way). Clivana's census: per_item 5, paginated 5, convergence 2, batch 1, retry 1.

`TX_COMMIT_INSIDE_BEGIN_BLOCK` (new, `high`, static, category transactions): `x.commit()` inside `with x.begin():` / `async with x.begin():` on the same receiver. Its first draft said that the statements after the inner commit "run in a transaction the block never commits". The PostgreSQL test written for the reference implementation (`tenant-ledger/tests/test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement`, SQLAlchemy 2.0.55, asyncpg) showed otherwise and the rule was corrected before release: the inner commit closes the block's transaction and the next statement raises `InvalidRequestError: Can't operate on closed transaction inside context manager`; the first delta is committed and the second never runs; the loop shape fails on its second iteration; without the inner commit both deltas are visible together after exit. The message, the catalogue summary, the remediation text and the reference document now say exactly that. When the commit is the block's last statement the finding is `medium` (`details.follows: false`: redundant today, a crash as soon as a statement is added). A commit inside `begin()` that is also inside a loop is reported once, by this rule.

## Remediation foundations (P5)

`fastapi_audit/remediation.py` attaches a `remediation` object to findings of `TX_COMMIT_IN_LOOP`, `TX_COMMIT_INSIDE_BEGIN_BLOCK`, `SA_ASYNC_SESSION_NOT_AWAITED`, `ASYNC_BLOCKING_CALL_INDIRECT` and `SEC_RESOURCE_AUTHZ_UNVERIFIED`: `problem`, `evidence` (file, line, function, symbol, call path, details, evidence state), `certainty` (`high` for `static_confirmed`, `medium` for `inferred`, `low` for `insufficient`/`partial`, whose first step is to confirm the finding), `proposed_fix` (ordered steps), `invariants`, `tests`, `regression_risk` (level and reason). `status` is always `proposed`, `applied` and `verified` always `false`; suppressed findings and the other 54 rules carry `remediation: null`. The auditor never edits code, emits no patch and claims no verification; a test asserts that an audited project's files are byte-identical before and after an audit. **Pending, deliberately**: applying a proposal and verifying it by running the listed tests are not implemented; nothing in this release should be read as an automatic fixer.

## Report comparison (P6)

`report.compare_reports` (used by `--baseline` and by `scripts/audit_diff.py BEFORE.json AFTER.json [--format text|json|markdown] [--fail-on-new] [--fail-on SEVERITY]`; the root script forwards to the skill's `audit_diff.py`): the v1.4 keys are unchanged; `changes` adds `no_longer_detected`, `persistent`, `severity_changes`, `confidence_changes`, `evidence_changes`, `newly_suppressed`, `unsuppressed`, `new_suppressed`, with the corresponding lists, `suppression_changes`, `partial` (`comparison_reliable` is false when either analysis was partial or the baseline's tool version is unknown), `baseline_tool_version`, `current_tool_version`, `tool_changed` and a `note`. A finding the current run does not report is **no longer detected**, never resolved; `no_longer_detected` excludes findings that are still reported but newly suppressed (listed under `suppression_changes`), while `fixed` keeps its v1.4 meaning. Clivana v1.7 → v1.8 through the tool: new 0, no longer detected 3, persistent 50 (one fingerprint is shared by two findings), no severity, confidence or evidence changes, reliable.

## Findings on the calibration repositories

| Repository | v1.7 | v1.8 | Preserved | New | No longer detected |
|---|---|---|---|---|---|
| Clivana | 55 | 52 | 51 of 51 fingerprints | 0 | 3 (the deliberate loops above) |
| AgentForge | 11 | 16 | 11 of 11 | 5 | 0 |
| tenant-ledger | 6 (7 with the new anti-patterns) | 8 | 6 of 6 | 2 (`TX_COMMIT_INSIDE_BEGIN_BLOCK`, deliberate) | 1 (the loop commit inside `begin()`, now reported by the new rule) |

The five new AgentForge findings, each inspected in the source: four `ASYNC_BLOCKING_CALL_INDIRECT` (`low`, `inferred`) in `scripts/sandbox_smoke.py` and `scripts/development_mvp.py`, where async entry points call `deps.workspaces.ensure(...)` / `.remove(...)`, synchronous methods of `WorktreeManager` that reach `subprocess.run` through `LocalGitAdapter._run`; v1.7 could not follow `deps.workspaces` (a two-level attribute chain on a typed parameter). They are development scripts, so their practical impact is the user's call; the rule's statement is true. One `SEC_RESOURCE_AUTHZ_UNVERIFIED` (`low`, `insufficient`) on `create_runner_app.cancel` (DELETE `/v1/executions/{execution_id}`): authenticated through the factory-local `Authenticated` alias, acting on the execution id through `ExecutionStore.request_cancel`, no visible ownership binding; a question, not a verdict. The sibling `admin_resolve` route is now classified `authenticated_authorization_visible` through its factory-local `Admin` alias (role) and asks nothing. No severity, confidence or evidence of a preserved finding changed on either repository.

## Tests, validation and benchmarks (P7, P10)

- `python3 -m unittest discover -s tests`: 292 tests (251 before, 41 new in `tests/test_audit_v18.py`, one assertion in `test_audit_v17.py` relaxed to accept later additive schemas and the rule count updated to 59 with a comment) on Python 3.13.5 and 3.9.6 (4 skips on 3.9: `jsonschema` not installed there, as in v1.7); `pytest -q tests` equivalent.
- `tenant-ledger`: 30 tests on a throwaway PostgreSQL 16 cluster (29 + the commit-inside-begin reproduction), `ruff check` and `ruff format --check` clean; the six previous audit fingerprints identical, two deliberate new findings.
- SARIF: `--format sarif` of the dev corpus and of Clivana validate against the vendored official OASIS 2.1.0 schema (`scripts/validate_sarif.py`, jsonschema 4.23 on 3.13); the new rule appears in the driver rules.
- Determinism: two CLI runs on Clivana produce byte-identical JSON (including `remediation`, `details`, `dynamic_by_origin`); the suite checks the same on the dev corpus.
- Configuration: the fail-closed validation and the declared-contracts fixtures are unchanged and green; `facts.commit_loops` and `remediation` do not depend on configuration.
- `scripts/validate_structure.py` (extended for `test_audit_v18.py`, `truth18/{dev,acceptance}`, the comparison tool and shim, and `remediation.py`), `claude plugin validate .`, `git diff --check` and a secrets scan of every changed file (key patterns, private keys, personal credential paths) pass.
- Benchmarks (`scripts/audit_precision.py --against e8224ce`, same machine, fastest of one run each; in-process measurements in parentheses):

| Project | Files | Call sites | v1.7 time | v1.8 time | v1.7 RSS | v1.8 RSS |
|---|---|---|---|---|---|---|
| Clivana | 457 | 27,310 | 17.7 s (11.7) | 18.5 s (12.6) | 105.9 MB | 107.9 MB |
| AgentForge | 288 | 14,996 | 9.9 s (6.9) | 10.5 s (7.0) | 64.9 MB | 65.4 MB |
| tenant-ledger | 38 | 776 | 0.43 s | 0.45 s | 33.4 MB | 28.0 MB |
| truth18/dev | 7 | 96 | 0.067 s | 0.068 s | 31.9 MB | 24.7 MB |

Cost: +4 to +8 % wall clock on the two real repositories (the new work is in the summaries and the graph; profiling shows the rule modules and fact collection still dominate), memory unchanged. No interprocedural pass was truncated.

## Compatibility (P9)

| Contract | Status |
|---|---|
| CLI options and exit codes | unchanged; `audit_diff.py` is a new script with its own codes (0 / 1 with `--fail-on-new` / 2 invalid input) |
| JSON 1.0–1.5 | every key kept; schema 1.6 adds `details`, `remediation`, `facts.commit_loops`, `interprocedural.coverage.{project,external,unresolved}_site_ratio`, `interprocedural.uncertainty.{callback_sites_unresolved,framework_callbacks,dynamic_by_origin}`, `interprocedural.{mixin_attribute_kinds,subclass_hooks,external_base_methods}`, `comparison.changes` and lists. `comparison.counts` keeps exactly `new/fixed/unchanged` (asserted) |
| Semantics corrected, not renamed | `uncertainty.callback_params_unresolved` now counts parameters and excludes framework callbacks (documented); `TX_COMMIT_IN_LOOP` no longer reports structurally deliberate loops (documented, three calibration findings) |
| SARIF 2.1.0 | unchanged structure; validates against the official schema; new rule listed |
| Declarative contracts and fail-closed configuration | unchanged; fixtures and tests green |
| Rule ids | 58 kept, 1 added (`TX_COMMIT_INSIDE_BEGIN_BLOCK`) |
| Fingerprints | identical for every preserved finding on Clivana (51/51), AgentForge (11/11) and the example (6/6); per-item loop messages unchanged, classification lives in `details` |
| Suppressions | unchanged; suppressed findings carry no remediation; the comparison reports suppression transitions |
| tenant-ledger | 30 tests green; clean report with `--exclude-path "*/antipatterns.py" --ignore MIG_RAW_SQL` |
| Evals | 47 cases unchanged, structure validated offline by `tests/test_plugin_evals.py`; no paid run |
| References | 25 documents, all linked; `audit-engine.md`, `audit-rules.md`, README, SKILL.md updated |

## Residual risks and limits

- The acceptance corpus is author-written and was scored twice (both results kept); it is not a blind holdout. A corpus written by someone else remains the better test.
- Closures inherit only the enclosing function's *declared* kinds, instances and callback kinds; kinds the parent only inferred from its callers are not inherited. Calls to functions defined inside the current function are summarised but not followed (`nested_function` origin).
- The batch heuristics read structure, not intent: `len(x) >= N` on a non-buffer or a counter threshold unrelated to batching would still classify a commit as `batch` and silence it; the census in `facts.commit_loops` keeps such loops visible.
- Subclass hooks and mixin kinds are `inferred`; a class hierarchy completed at runtime (plugins, registries) can add implementations the engine does not see.
- The external-base rule assumes that an unknown method of a class deriving from a third-party, stdlib or builtin class lives in that base; a typo in a method name on such a class is classified `external`, not `not_found`.
- Remediation blocks are proposals; nothing applies or verifies them, and `certainty` reflects the evidence state, not a measured fix success rate.
- The four new AgentForge findings sit in development scripts; the rule is right about the event loop, the scripts may not care.
- `--no-interprocedural` keeps the loop classification and the new rule (tested) but loses the attribute, callback and hook resolution, as in v1.7.

## Review before publication (second delivery, 2026-10-30)

Scope requested: loop commits and atomicity, evaluation hygiene, the AgentForge discrepancy, the remediation engine, the comparison tool, final verifications. The nine commits of the first delivery are kept; the review adds thematic commits on top.

### Loops and atomicity

Positive and negative tests (`LoopAtomicityTests`) show that classifying a loop as deliberate silences only the loop-commit finding, never the other transactional rules: an HTTP call inside the transaction of a paginated loop (`TX_EXTERNAL_CALL_IN_TRANSACTION`), a read-modify-write inside a modulo batch (`TX_READ_MODIFY_WRITE`), a session shared by `gather` inside a convergence loop (`SA_SESSION_SHARED_CONCURRENTLY`) and a non-idempotent HTTP write inside a retry loop (`RETRY_NON_IDEMPOTENT_WRITE`) are all reported; a per-item inner loop inside a paginated outer loop, a per-item commit next to a modulo-guarded one, and a modulo test that does not guard the commit are reported as `per_item`. `facts.commit_loops` counts every kind in every mode.

Opt-in policy: `"transactions": {"single_transaction_per_use_case": true}` in `.audit-fastapi.json` reports the `batch`, `paginated` and `convergence` loops as `TX_COMMIT_IN_LOOP` with a distinct message, confidence one step below the per-item finding and `details.policy`; per-item findings keep message and fingerprint; retry loops are never policy findings; the section fails closed like `contracts` (non-boolean value or unknown property: exit 2 with the JSON path, `x-` keys allowed). Default off; the default report is byte-identical to the first delivery.

### Evaluation (all historical results kept)

| Corpus | v1.7.0 | v1.8.0 | Status |
|---|---|---|---|
| `truth18/dev` | P 0.333 / R 0.286 / F1 0.308; edges 1/6; unresolved 7/16 | 1.0 / 1.0 / 1.0; 6/6; 16/16 | used during implementation; scored in-process by the suite |
| `truth18/acceptance`, first scoring | 0.25 / 0.2 / 0.222; 1/4; 5/9 | 1.0 / 0.8 / 0.889; 4/4; 8/9 | author-written, displayed once, edited once before scoring |
| `truth18/acceptance`, second scoring | same | 1.0 / 1.0 / 1.0; 4/4; 9/9 | after two closure fixes prompted by the first scoring; not a blind test |
| `truth18/holdout` (new, 5 files, 33 markers) | 0.375 / 0.333 / 0.353; 1/4; 6/12 | 0.8 / 0.889 / 0.842; 4/4; 12/12 | written before the review changes, scored once with the final engine, **not edited**, results not used to change any rule |

Holdout open items, analysed and left as they are: the two "false positives" (`ASYNC_BLOCKING_CALL_INDIRECT` at `wms/layers.py:25` and `wms/picking.py:40`) are true findings the expectations omitted: `Handler.handle` and `Picker.describe` are `async def` and call, through a resolved edge, a method that sleeps; the markers only asked for the edge. The "false negative" at `wms/picking.py:48` (`route_like`, same blocking callee) follows from the rule's documented "one finding per blocking helper per file" deduplication: the helper was already reported at line 40. The remaining 17 markers for P1–P4 hold, including the `follows=True/False` grading of the begin-block rule, the inner per-item loop inside a paginated loop and the HTTP call inside a paginated transaction. `truth17/holdout` was not used.

### AgentForge, reconciled

The first delivery said "four" in one place and "five" in another because the four indirect-blocking findings were joined by one resource question. After the review the count is **four**:

| Rule | Location | Severity | Evidence / state | Confidence | Code | Impact | Classification |
|---|---|---|---|---|---|---|---|
| `ASYNC_BLOCKING_CALL_INDIRECT` | `scripts/sandbox_smoke.py:222` (`run_smoke` → `deps.workspaces.ensure`) | medium | inferred (one link through the `GitRepositoryPort` interface with a single implementation) | low | operator script (`python -m scripts.sandbox_smoke`; "never part of the application") | the event loop blocks while git runs; the script awaits nothing else concurrently, so no observable effect today | supported signal, low impact |
| `ASYNC_BLOCKING_CALL_INDIRECT` | `scripts/sandbox_smoke.py:313` (`run_smoke` → `deps.workspaces.remove`) | medium | inferred | low | operator script | same | supported signal, low impact |
| `ASYNC_BLOCKING_CALL_INDIRECT` | `scripts/development_mvp.py:740` (`run_development` → `deps.workspaces.ensure`) | medium | inferred | low | operator script | same | supported signal, low impact |
| `ASYNC_BLOCKING_CALL_INDIRECT` | `scripts/development_mvp.py:863` (`run_development` → `deps.workspaces.remove`) | medium | inferred | low | operator script | same | supported signal, low impact |
| `SEC_RESOURCE_AUTHZ_UNVERIFIED` (first delivery only) | `runner/agentforge_runner/service.py:175` (`create_runner_app.cancel`) | medium | unknown / insufficient | low | production (runner service) | none: `authenticated(request) -> None` compares a shared bearer token and hands the route no principal; there is one caller identity and nothing a resource could be bound to | **false positive by design**, withdrawn |

Correction with regression tests: route contracts carry `principal_kind` (`value` / `none` / `unknown`); when every authentication parameter is unused (`_: Authenticated`) and every resolvable dependency behind it is annotated `-> None` and never writes `request.state`, the kind is `none` and the resource question is not asked. Providers defined inside the same factory now resolve to their nested record, which is what made the AgentForge case visible. A `-> None` dependency that stores `request.state.user`, or one that returns a value, keeps the question (tested). On AgentForge 8 routes are `none`, 9 `value`, 2 `unknown`; on Clivana 149 `value`, 18 `unknown`, none withdrawn. Final counts: Clivana 55 → 52 (51 fingerprints preserved, nothing changed on them), AgentForge 11 → 15 (11 preserved), tenant-ledger 6 → 8 (6 preserved; the loop commit inside `begin()` is now reported by the new rule only). Per-site: 0 project edges retargeted, 0 lost, 0 classified sites became dynamic.

### Remediation engine, comparison, SARIF

`RemediationWordingTests` adds to the existing guarantees: every proposal names at least one test and one invariant, states a regression risk with its reason, and none of the texts promises a resolution ("resolves the", "fixes the defect", "guarantees", "is now verified", ... are rejected); `status`/`applied`/`verified` stay `proposed`/`false`/`false` and audited files are byte-identical after an audit. SARIF results now carry `details` and `remediation` in their property bag; the documents of the dev corpus and of Clivana validate against the official OASIS 2.1.0 schema (new test). The comparison tool is additionally tested against a pre-1.5 baseline without fingerprints or evidence states (fingerprints recomputed, no evidence change invented, reliability depends on the baseline naming its tool version).

### Final verifications

303 tests on Python 3.13.5 and 3.9.6 (5 skips on 3.9: `jsonschema`), `pytest` equivalent; tenant-ledger 30 tests on PostgreSQL unchanged since the first delivery (no example file changed in the review); architectural and declared contracts fixtures green; SARIF against the official schema; JSON byte-identical across runs; `claude plugin validate`, `scripts/validate_structure.py` (now requiring the holdout), `git diff --check` and the secrets scan pass. No paid evaluation was run. The review re-measurements of time were taken on a machine under heavy unrelated load (load average above 30), so the benchmark table of the first delivery remains the reference.

## Publication recommendation

Ready to publish as v1.8.0 after review of this document, of the three Clivana loops and the four AgentForge findings named above, with `git push origin main` (no `--force`), from the local commits listed by `git log --oneline e8224ce..HEAD`. No tag or release was created and nothing was pushed.
