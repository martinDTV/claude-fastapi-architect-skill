# Safe refactoring recipes and verified remediation (v1.9)

The auditor reports; `scripts/audit_remediate.py` proposes, applies **in isolated copies**, and verifies. It never writes to the audited project: the output is a state per finding, with its evidence, and a unified diff a human applies with `git apply`. This document is the contract behind each state, the trust model behind every execution, and the manual recipe behind each automatic one.

## Trust model: what runs, where, and when

| Step | Executes project code? | Needs |
|---|---|---|
| Audit of the project and of each copy | no | nothing |
| Candidate patch (AST preconditions, unified diff, parse check) | no | nothing |
| Re-audit of the patched copy, regression check, test-file digest | no | nothing |
| Linked reproducer before/after, suite in the combined copy | **yes** | `--authorize-execution` **and** a trust profile with a scope that contains the project **and** a registered executor that accepts that profile |

Trust profiles: `fixture` (the plugin's own fixtures and examples), `trusted` (a project the operator declares trustworthy), `untrusted` (the default). The declaration is recorded in the run report (`authorization.declared_by`, `technical_guarantee: false`); **it is a statement by the operator, not a security property**. Only one executor ships: `local` (`isolation_level: process_hygiene`). It accepts `fixture` and `trusted`; for `untrusted` the run fails closed (`execution.denied_code: executor_unavailable`, every case at most `audit_clean`) until an executor with `isolation_level: isolated` is registered. `Executor` (`fastapi_audit/remediate/executors.py`) is the seam for such a runner: it declares its isolation level, its guarantees and what it does not provide, decides which profiles it accepts, and runs a command in a workspace.

What the local executor guarantees (each enforced and tested): the copy excludes `.git`, virtual environments, caches and build output; the command is split without a shell and its program must be an interpreter (`python`, `python3`, `python3.x`), `pytest` or `uv` (extend with `--allow-command`); it runs with the copy as working directory, a fresh `HOME` inside the workspace, only the pass-through variables (`PATH`, locale, `TMPDIR`; add names with `--env-passthrough`, for example `UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR`), stdin closed, a wall-clock timeout, CPU-time and file-size limits on POSIX; the project's digest is compared before and after the run.

What it does **not** provide, and the report repeats on every case: network isolation; filesystem confinement beyond the fresh `HOME` (the tests can write anywhere the user can); memory limits; protection against a malicious test suite, which runs with the operator's privileges. Filtering command text is not a sandbox and is not presented as one: the program allow-list only narrows *what* is launched, not what the launched tests may do. A project whose tests you would not run on this machine must be remediated inside a container or not at all; `--trust trusted` does not change that.

## What counts as a test run (runner adapters)

An exit code is not evidence. The executor instruments every declared command with a **runner adapter** that makes the runner write a structured report bound to the run by a nonce, and reads it back: tests discovered (when the runner knows), selected, executed, passed, failed, errored, skipped, and collection errors, with the ids of each.

| Runner | Adapter | Report | Bound by | Notes |
|---|---|---|---|---|
| pytest (`pytest`, `python -m pytest`, `uv run ... pytest`) | `pytest-junit` | `--junit-xml` (xunit2) | `junit_suite_name=<nonce>` | `errors` are fixture/collection problems (infrastructure); `discovered` is not reported by JUnit (deselected tests are invisible) |
| unittest (`python -m unittest ...`) | `unittest-json` | the tool's own runner (`_unittest_runner.py`, standard library) writes JSON | `nonce` field | an exception raised by a test is a failed test (unittest semantics); loader failures are collection errors |
| anything else | none | — | — | outcome `unverifiable`: the run may execute (when allowed) but can never verify |

Outcome rules, in order: refused, not run, timeout; no adapter → `unverifiable`; report absent → `no_evidence`; report unparseable → `report_corrupt`; nonce or timestamp mismatch → `report_mismatch`; any collection error → `infrastructure_error`; nothing executed → `no_tests`; only skips → `skipped_only`; any failed test → `failed`; errors without failures → `infrastructure_error`; exit code non-zero with every listed test passed → `infrastructure_error`; otherwise `passed`. Summary lines printed by the runner are kept as `text_counts` for the reader and never used for a verdict. Negative controls in `tests/test_audit_v19.py`: a script that prints a pytest-looking summary and exits 0, an empty test class, a skipped-only selection, a truncated report, a report with another run's nonce, a report older than the run, a run whose report was deleted.

## Linking a finding to its reproducer

`--link FINGERPRINT=CMD` and `--link FILE:LINE=CMD` are specific to one case (`reproducer_scope: case`). `--link RULE=CMD` is **shared** by every case of that rule that has no more specific link (`reproducer_scope: rule`, `shared_with` lists the others, and a limitation says so). Sharing is allowed because the verdict never comes from the command's exit status but from **tests that change state in the case's own copy**: `flipped_tests` are the ids that failed (or errored) before the patch and passed after it; `regressed_tests` are the ids that passed before and fail after. `verified` requires at least one flipped test and no regressed test. A shared reproducer that covers only one of two findings of the same rule therefore verifies that one (its tests flip in its copy) and leaves the other `inconclusive: reproducer_not_specific` (its tests fail before and after in its copy, unchanged); a reproducer that encodes the old defective behaviour next to a flipping test rejects the candidate (`tests_failed_after` with the regressed ids). This is the demonstrable relation between a case, its candidate and its reproducer, and it is tested with two findings of one rule where only one is covered.

## Composition of candidates

Individually verified candidates are never assumed to work together. The composition stage applies them to **one more copy**, in file order from the highest line down, recording for each the candidate id and the file digest before and after (`combined.applied`, with `stacked_on_previous_candidate` when a file receives more than one), audits the composed copy (every target gone and not suppressed, nothing new), runs **each verified case's reproducer again** there, and runs the declared regression suite of the corrected code (`--test-command`) before and after. Result (`combined.state`, `summary.composition`): `verified` only when every composed candidate is individually verified, the audit is clean, every reproducer passes in the composed copy and the suite passes with verifiable evidence; `incompatible` when the audit or a reproducer shows the candidates do not work together; `inconclusive` otherwise, with its cause (no execution, no suite declared, candidates without individual verification, the suite failing with the ids that failed). A non-verified composition is attached as a limitation to every composed candidate; the individual verdicts stand.

On `tenant-ledger` the first composition attempt failed one test: `test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement`, a historical test that asserts the defective behaviour on purpose (it documents the raise). It was not deleted or silenced: it carries the `antipattern_demo` marker, keeps running in the default suite against the anti-pattern code, and the regression suite of the corrected code is declared as `pytest -q -m "remediation or not antipattern_demo"`. With that suite the composition of the four candidates is `verified` (34 of 34 after, 29 of 34 before, every reproducer passing in the composed copy).

## States and their evidence

| State | Claim | Required evidence | `reason_code` values |
|---|---|---|---|
| `proposed` | a recipe exists | the audit report (`remediation.recipe`) | |
| `blocked` | no safe automatic patch | a failed precondition | `precondition_failed` |
| `patched` | the candidate is applied in the case's copy | intermediate only | |
| `audit_clean` | after the patch the finding is **no longer detected** (not even as suppressed) and **no new finding** appeared anywhere in the copy; **no test executed** | two audits of the copy compared by fingerprint | `execution_unauthorized`, `executor_unavailable`, `scope_mismatch` |
| `verified` | `audit_clean` **and** the case's linked reproducer, read through a runner adapter, shows at least one test that **failed before and passed after** the patch and none that regressed, run by a named executor, with test files untouched | the two runs with their nonce-bound reports (counts and test ids), `flipped_tests`, `regressed_tests`; the model refuses the transition when anything is missing | `verified` |
| `rejected` | the candidate is wrong or invalid | the check that failed | `not_reproduced`, `stale_baseline`, `candidate_mismatch`, `patch_failed`, `finding_persists` (active or suppressed), `tests_modified`, `regression_introduced`, `tests_failed_after` |
| `inconclusive` | the evidence does not support a verdict; **never a defect of the patch** | the run that could not decide | `no_linked_tests`, `defect_not_reproduced`, `reproducer_not_specific`, `tests_skipped`, `no_tests`, `infrastructure_error`, `timeout`, `command_refused`, `runner_unverifiable`, `evidence_missing`, `report_corrupt`, `report_mismatch` |

Rules the pipeline enforces: `audit_clean` never equals `verified`; a test that did not run never counts as passed; a skipped test or an empty collection never satisfies an obligation; a baseline change (`base_sha256` of the audited file differs from the copy) invalidates the candidate; runner-internal, usage and fixture-setup errors (a database that did not start, a selector that does not resolve) are infrastructure, not verdicts; `verified` is refused by the model itself (`missing_evidence_for_verified`) when any piece is missing. The historical v1.8 `remediation` block of the audit report keeps its meaning (`status: proposed`, `applied: false`, `verified: false`) and only gains `recipe`, `automatable`, `tool`.

## Evidence per finding

Each case carries: the finding (rule, file, line, function, fingerprint); `base_sha256` of the audited file; the candidate (`candidate_id`, recipe, edits, diff, original and patched digests); the reproducer (command, `reproducer_scope`, `shared_with`) with its run **before** and **after** (instrumented command, exit status, adapter, nonce-bound report with counts and test ids, outcome, executor); `flipped_tests` and `regressed_tests`; the two audits of the copy (target present / suppressed, new findings); `regressions`; `invariants` with how each was checked (`audit`, `parse`, `linked_tests`, `not_checked`) and whether it held; `tests_untouched`; the executor and its isolation level; `limitations` (what the executor does not provide, shared reproducers, test code, a non-verified composition); the state, its reason code and the full history. A case without a link is `inconclusive` (`no_linked_tests`) when execution was granted: a suite alone cannot attribute a verdict to one finding.

## Recipes

Each recipe states preconditions (checked on the AST; a failure blocks the case), the change, the invariants that must hold and how the pipeline checks them, the tests that demonstrate them, and the rollback (always: discard the copy; nothing happened to the project).

### `remove_commit_inside_begin` — `TX_COMMIT_INSIDE_BEGIN_BLOCK` (maturity: postgresql-verified)

- Preconditions: the `commit()` is an expression statement alone on its line inside `with x.begin()` / `async with x.begin()`; the file parses before and after.
- Change: delete the statement (replace by `pass` when it was the only statement of its block).
- Invariants: every statement of the block runs inside the transaction the block owns; all writes of the block are visible together after exit and absent together after an exception; no `InvalidRequestError` on the statement that followed the commit. Checked by: audit (finding gone, nothing new), parse, linked tests.
- PostgreSQL evidence: `examples/tenant-ledger/tests/test_remediation_targets.py::test_commit_inside_begin_is_remediated` and `::test_commit_per_item_inside_begin_is_remediated` fail on the anti-pattern (SQLAlchemy 2.0.55 raises on the statement after the inner commit) and pass after the recipe.
- Manual variant: if the early commit was intended, drop the `begin()` block and manage the transaction explicitly.

### `add_await` — `SA_ASYNC_SESSION_NOT_AWAITED` (maturity: postgresql-verified)

- Preconditions: the call is inside an `async def` (not a lambda or comprehension), not already awaited; the file parses after inserting `await`. A synchronous function is blocked (manual recipe below).
- Change: `session.execute(...)` → `await session.execute(...)` (also `return await ...` and `x = await ...`).
- Invariants: the statement executes exactly once; no `coroutine was never awaited` warning; **exceptions raised by the statement now propagate at the call site** (a documented behaviour change: before the fix they could not, because nothing ran).
- PostgreSQL evidence: `::test_note_without_await_executes_exactly_once_without_warnings` (entry count +1, no warning after `gc.collect()`) and `::test_note_without_await_surfaces_database_errors_after_the_fix` (a foreign-key violation raises after the fix) fail on the anti-pattern and pass after the recipe.
- Negative controls: `add_await` is blocked inside `def`, inside lambdas and comprehensions, and when the call is already awaited (unit tests); a candidate that leaves the finding, suppresses it, edits a test or introduces another un-awaited call is rejected (adversarial tests).
- Manual variant (blocked case): make the function `async def` and await it up the chain, or use a synchronous `Session`.

### `move_commit_after_loop` — `TX_COMMIT_IN_LOOP` with `loop_kind: per_item` (maturity: postgresql-verified for the shape tested; changes failure semantics)

- Preconditions: the commit is a direct child of the loop body, alone on its line; the loop contains no `return` and defines no function; the loop reported by the audit matches the source. Deliberate loops (`batch`, `paginated`, `convergence`, and loops reported only under the opt-in policy) have **no** automatic recipe: changing their transaction boundary is a design decision.
- Change: delete the per-item commit and insert the same statement after the loop at the loop's indentation (only delete when a commit on the same receiver already follows the loop).
- Invariants: either every item is committed or none is; a failure half-way leaves nothing committed; the committed rows after a successful run are unchanged. This is a deliberate change of failure semantics (all or nothing instead of partial progress); the recipe refuses loops with `return`, where it would also change the success path.
- PostgreSQL evidence: `::test_deposit_each_is_atomic_when_an_amount_is_rejected` (two accepted amounts followed by one the `CHECK (balance >= 0)` constraint rejects: before the fix the balance moved by the first two, after the fix it is unchanged and the error propagates) fails on the anti-pattern and passes after the recipe. Shapes beyond this one (per-item work with side effects outside the database, loops whose body sets per-item transaction context) are covered only by the fake session in the fixture and by the invariants above; treat them as needing their own reproducer.
- Manual variant: when partial progress is wanted, batch explicitly under a visible size test and make the per-item write idempotent (then the loop is `batch` and the auditor is silent).

### Manual-only recipes (reported with `automatable: false`)

- `ASYNC_BLOCKING_CALL_INDIRECT`: offload the helper (`anyio.to_thread.run_sync`), make the chain async, or declare the route with plain `def`; test concurrent latency.
- `SEC_RESOURCE_AUTHZ_UNVERIFIED`: load the resource through a dependency that receives the principal, or scope the repository call by the principal; test as an unrelated user.

## Capability matrix

| Rule | Proposal in the audit report | Structured patch | Reproduction | Verified with fakes (`tests/fixtures/remediation19`) | Verified with PostgreSQL (`tenant-ledger`) | Execution safety | Known limits |
|---|---|---|---|---|---|---|---|
| `TX_COMMIT_INSIDE_BEGIN_BLOCK` | yes | `remove_commit_inside_begin` | fixture test + two PostgreSQL targets | yes | yes (statement after the commit; loop shape) | local executor only (process hygiene) | commits that share a line or sit in larger expressions are blocked |
| `SA_ASYNC_SESSION_NOT_AWAITED` | yes | `add_await` | fixture test + two PostgreSQL targets | yes | yes (executed once, no warning, errors surface) | local executor only | synchronous functions, lambdas and comprehensions are blocked; errors now propagate (documented) |
| `TX_COMMIT_IN_LOOP` (`per_item`) | yes | `move_commit_after_loop` | fixture test + one PostgreSQL target | yes | yes (atomicity under a rejected amount) | local executor only | loops with `return`, nested commits and deliberate kinds are blocked; all-or-nothing semantics |
| `ASYNC_BLOCKING_CALL_INDIRECT` | yes | no (manual) | no | no | no | not applicable | — |
| `SEC_RESOURCE_AUTHZ_UNVERIFIED` | yes | no (manual) | no | no | no | not applicable | — |
| other 54 rules | no | no | no | no | no | not applicable | — |

## Using the tool

```
python3 scripts/audit_remediate.py PROJECT --select TX_COMMIT_INSIDE_BEGIN_BLOCK                       # static: audit_clean at best, nothing executed
python3 scripts/audit_remediate.py PROJECT --authorize-execution --trust trusted --trust-scope PROJECT \
    --link "src/app/x.py:42=pytest -q tests/test_x.py::test_commit_once" \
    --test-command "pytest -q -m 'not antipattern_demo'" --patch-dir patches --output run.json --strict          # the regression suite of the corrected code
git apply patches/TX_COMMIT_IN_LOOP-<fingerprint>-<candidate>.diff                                   # the human step
```

`--format json` prints the run report (`remediation_run/1.2`), `--keep-workspace` leaves the copies for inspection, `--fail-on-rejected` and `--strict` gate CI (`--strict` fails unless every case is `verified`). The audit report's `remediation` block names the recipe (`recipe`, `automatable`, `tool`).

## Towards v2.0

An executor with `isolation_level: isolated` (container or microVM: network off, read-only mounts, memory limits) registered behind the same `Executor` interface so untrusted projects stop failing closed; automatic discovery of reproducers from per-test results; recipes for more rules (offloading a blocking helper, adding a timeout, closing a session); an explicit `apply` step that writes to the project only after a recorded human approval; and runtime records (`evidence_state: reproduced`) fed back into the audit report from a remediation run.
