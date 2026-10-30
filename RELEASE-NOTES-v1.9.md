# FastAPI Architect v1.9 — Verified Remediation & Safe Refactoring

Additive release on top of v1.8.0 (`cffd3bd`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id (59), the suppression markers, the fingerprints, the comparison tool and the opt-in loop policy. Report schema 1.7 is a superset of 1.0–1.6: the `remediation` block of a finding gains `recipe`, `automatable` and `tool`; its v1.8 fields keep their meaning (`status: proposed`, `applied: false`, `verified: false`). The remediation run is a separate artefact (`remediation_run/1.1`).

## Scope and the brief

The detailed v1.9.0 brief was not available in the implementation session; the first delivery followed the ten points of the request as given and the hardening review (below) the acceptance criteria that followed. Where the brief may have fixed details the request did not repeat (isolation mechanism, artefact format, rule choice), the decisions are documented and reversible.

| Point | Status | Where |
|---|---|---|
| 1. Remediation model with verifiable states | complete: `proposed`, `blocked`, `patched`, `audit_clean`, `verified`, `rejected`, `inconclusive`; closed vocabulary of reason codes; transitions enforced; `verified` refused by the model when evidence is missing | `fastapi_audit/remediate/model.py` |
| 2. Automated reproduction in fixtures | complete: `tests/fixtures/remediation19` (fake session faithful to SQLAlchemy's semantics; three failing tests become five passing) and five PostgreSQL targets in `tenant-ledger`; each case links one reproducer and records whether it failed before the patch | `tests/fixtures/remediation19`, `examples/tenant-ledger/tests/test_remediation_targets.py` |
| 3. Structured patch proposals | complete: line-level edits plus unified diff, base and patched digests, candidate id, preconditions on the AST, recipe metadata (invariants, maturity, failure-semantics flag) | `remediate/patches.py` |
| 4. Execution only in isolated, authorized environments | complete within the stated model: static steps never execute project code; execution needs authorization + trust profile + scope + an executor that accepts the profile; the local executor (process hygiene) accepts `fixture`/`trusted` and refuses `untrusted` (fail closed); `Executor` is the seam for an isolated runner; limits documented on every case | `remediate/executors.py`, `remediate/isolation.py` |
| 5. Verification before and after by tests and audits | complete, per case: audit of the copy, linked reproducer before, patch, test-file digest, audit after with regression check, reproducer after; combined copy with the suite | `remediate/pipeline.py` |
| 6. Safe refactoring recipes | complete for the three automatic recipes and two manual ones, with preconditions, invariants, tests, rollback, maturity and a capability matrix | `references/safe-refactoring.md` |
| 7. Executable examples with FastAPI, SQLAlchemy and PostgreSQL | complete for the three recipes: four findings of `tenant-ledger` verified on a throwaway cluster, one copy and one reproducer each | `examples/tenant-ledger/VERIFICATION.md` |
| 8. Compatibility with JSON, SARIF and the 59 rules | complete: schema 1.7 additive, SARIF unchanged, rules untouched, v1.8 proposal contract asserted | compatibility tests of v1.4–v1.9 |
| 9. Adversarial evaluations and negative controls | complete: 39 tests in `tests/test_audit_v19.py` (one opt-in PostgreSQL) and one plugin eval case | `tests/test_audit_v19.py`, `evals/remediation-isolated-copy-only` |
| 10. Documentation and preparation for v2.0 | complete | `references/safe-refactoring.md`, `references/audit-engine.md`, README, SKILL.md, this document |

## Hardening review (second delivery)

Gaps found in the first delivery, verified on disk before changing code, and what closed them:

| Gap | Correction |
|---|---|
| Any project could execute tests once `--authorize-execution` was passed; no trust profile, no scope, no executor abstraction | `Authorization(execution, trust, scope, declared_by)`; `Executor` interface with `isolation_level`, `guarantees`, `not_provided`, `accepts(trust)`; `LocalProcessExecutor` (`process_hygiene`) accepts `fixture` and `trusted` only; `select_executor` fails closed (`executor_unavailable`, `scope_mismatch`, `execution_unauthorized`); the report records `technical_guarantee: false` for the declaration |
| Timeouts, refused commands and runner failures ended as `rejected` or `audit_clean`; a passing suite verified every case at once | `inconclusive` state with reason codes; `interpret_run` reads pytest and unittest conventions (exit codes, counts, `no tests ran`, skipped-only, fixture/collection errors, unresolved selectors) so infrastructure never becomes a verdict; one copy and one linked reproducer per case; `verified` requires the reproducer to fail before and pass after |
| No per-finding evidence: no base digest, candidate id, reproducer, linked tests, invariants or limitations | the case contract below; `candidate_mismatch` and `stale_baseline` checks; test files digested around the patch (`tests_modified`); suppression after the patch counts as `finding_persists` |
| Only the begin-block recipe had a PostgreSQL scenario | `note_without_await` (dropped `execute()`) and `deposit_each` (per-item commits) anti-patterns with four new targets; all three recipes verified on PostgreSQL |
| Adversarial controls missing | fourteen controls (list below), each with a distinct state and reason code |
| No overhead measurement | table below |
| Documentation presented the copy as "isolated" without a capability matrix | trust model, states, evidence contract, capability matrix and the local executor's limits in `references/safe-refactoring.md`; README and the engine reference aligned |

## Trust model and executors

| Step | Executes project code | Requirement |
|---|---|---|
| audit, candidate patch, re-audit, regression and test-file checks | no | none |
| linked reproducer before/after, suite in the combined copy | yes | `--authorize-execution` + `--trust fixture|trusted` + a scope containing the project + a registered executor that accepts the profile |

Local executor guarantees (enforced, tested): copy without `.git`, environments, caches and build output; no shell, program allow-list (`python*`, `pytest`, `uv`, extensible); fresh `HOME`; environment reduced to a pass-through list; stdin closed; timeout; CPU-time and file-size limits on POSIX; project digest compared before and after. Not provided (stated on every case): network isolation, filesystem confinement beyond the fresh HOME, memory limits, protection against a malicious suite. Command-text filtering is not presented as a sandbox: the program allow-list narrows what is launched, not what the launched tests may do. `untrusted` (default) is denied: `execution.denied_code: executor_unavailable`, every case at most `audit_clean`.

## States, reason codes and the evidence of a case

Final states: `blocked`, `audit_clean`, `verified`, `rejected`, `inconclusive` (`proposed` and `patched` are intermediate). Reason codes: `precondition_failed`; `execution_unauthorized`, `executor_unavailable`, `scope_mismatch`; `verified`; `not_reproduced`, `stale_baseline`, `candidate_mismatch`, `patch_failed`, `finding_persists`, `tests_modified`, `regression_introduced`, `tests_failed_after`; `no_linked_tests`, `defect_not_reproduced`, `tests_skipped`, `no_tests`, `infrastructure_error`, `timeout`, `command_refused`.

Guaranteed by the model and the pipeline, each with a test: `audit_clean` never equals `verified`; a test that did not run never counts as passed; a skipped test or an empty collection never satisfies an obligation; a baseline change invalidates the candidate; an infrastructure error is `inconclusive`, never a defect of the patch; `verified` is refused by `RemediationCase.transition` when any evidence is missing (twelve missing-evidence scenarios tested); the v1.8 proposal block keeps its meaning.

Per case: finding identity and fingerprint; `base_sha256`; `candidate_id` with the diff and both digests; `reproducer`; `linked_tests.before` / `after` (exit status, runner, counts, outcome, executor, output tails); `defect_reproduced`; `verification.before_audit` / `after_audit` (target present, target suppressed, new findings); `invariants` with `checked_by` and `satisfied`; `regressions`; `tests_untouched`; `executor` and `isolation_level`; `limitations`; `state`, `reason_code`, `reason`, `history`. `applied_to_project` is always `false`.

## Evidence

- Fixture (`tests/fixtures/remediation19`), defective code: 5 tests, 3 fail. Static run: 3 `audit_clean` / `execution_unauthorized`, nothing executed. Authorized but `untrusted`: 3 `audit_clean` / `executor_unavailable`, nothing executed. `fixture` trust without links: 3 `inconclusive` / `no_linked_tests`. `fixture` trust with one linked reproducer per rule and the suite as combined command: 3 `verified`, each reproducer failed before and passed after, all invariants satisfied, suite failed before and passed after in the combined copy (`composable: true`), fixture digest unchanged; the three diffs apply with `git apply` to the untouched fixture and the suite then passes.
- PostgreSQL 16 (throwaway cluster, SQLAlchemy 2.0.55, asyncpg), `tenant-ledger`: five deselected targets fail on the anti-patterns. The tool, with `--trust fixture`, one copy per case and `uv run --frozen --extra dev pytest -q -m remediation ... -k <target>` linked per finding (`UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` passed through):

| Case | Recipe | Reproducer before | after | Verdict |
|---|---|---|---|---|
| `antipatterns.py:176` `TX_COMMIT_INSIDE_BEGIN_BLOCK` | `remove_commit_inside_begin` | 1 failed (InvalidRequestError after the inner commit) | 1 passed (both deltas committed together) | `verified` |
| `antipatterns.py:190` `TX_COMMIT_INSIDE_BEGIN_BLOCK` (loop shape) | `remove_commit_inside_begin` | 1 failed | 1 passed | `verified` |
| `antipatterns.py:212` `SA_ASYNC_SESSION_NOT_AWAITED` | `add_await` | 2 failed (no entry written, `never awaited` warning; FK violation swallowed) | 2 passed (one entry, no warning; FK error raised) | `verified` |
| `antipatterns.py:224` `TX_COMMIT_IN_LOOP` (per item) | `move_commit_after_loop` | 1 failed (balance moved by the two accepted amounts) | 1 passed (balance unchanged after the rejected amount) | `verified` |

  Combined copy with all four candidates: audit clean, suite before 30 passed, suite after 29 passed and 1 failed: `test_antipatterns.py::test_commit_inside_begin_block_raises_on_the_next_statement`, which asserts the anti-pattern itself. The tool reported `composable: false` and attached the limitation to every candidate rather than hiding it; the individual verdicts stand. 73 s wall clock, 112 MB peak RSS (five environments synced from the local uv cache, five clusters). Project untouched.
- Adversarial controls (`tests/test_audit_v19.py`), each with its distinct outcome: suppressing the finding with a marker instead of fixing it → `rejected` / `finding_persists` ("suppressed rather than fixed"); editing an assertion in a test file → `rejected` / `tests_modified`; introducing another un-awaited call → `rejected` / `regression_introduced`; a reproducer that already passed before → `inconclusive` / `defect_not_reproduced` (the after run is not even attempted); a reproducer that fails after → `rejected` / `tests_failed_after`; an after run that does not execute → `inconclusive` / `command_refused`; a skipped test, an unresolved selector, a pytest run that collected nothing → `inconclusive` / `tests_skipped`, `infrastructure_error`, `no_tests`; a fixture error such as a database that did not start (pytest "1 error"), a runner internal error (exit 3), a timeout → `inconclusive` / `infrastructure_error`, `timeout`; a copy whose file drifted from the audited baseline → `rejected` / `stale_baseline`; a candidate for another file or another base → `rejected` / `candidate_mismatch`; a candidate that changes nothing → `rejected` / `finding_persists`; a scope that does not contain the project, an unregistered executor, an untrusted profile → execution denied with `scope_mismatch` / `executor_unavailable`; an invalid `.audit-fastapi.json` → the run stops before any copy; a project that changes during the run → `RemediationError`.
- Interpretation unit tests cover pytest exit codes 0–5 with counts (failed, errors-only, skipped-only, no tests) and unittest summaries (OK, FAILED, `Ran 0 tests`, skipped-only, `_FailedTest`, usage errors, missing summary), plus timeouts and refusals.

## Performance (reproducible: `tests/fixtures/remediation19`, best of three, children's peak RSS)

| Scenario | Wall clock | Peak RSS |
|---|---|---|
| audit only (`audit_fastapi.py`) | 0.11 s | 25.7 MB |
| remediation, static (no execution) | 0.32 s | 26.8 MB |
| remediation, per-case verified (3 cases, 2 audits and 2 reproducer runs each, plus the combined copy and suite) | 2.12 s | 47.9 MB |

The overhead is the copies and the extra audits; on `tenant-ledger` the cost is dominated by syncing one virtual environment and starting one PostgreSQL cluster per copy (73 s for four cases plus the combined copy). No performance improvement is claimed anywhere in this release.

## Tests and validation

342 tests on Python 3.13.5 and 3.9.6 (the 327 of v1.8.0 plus 15 net new; `tests/test_audit_v19.py` has 39, one of them the opt-in PostgreSQL pipeline test behind `FA_RUN_PG_REMEDIATION=1`, run by hand; 5 further skips on 3.9: `jsonschema`). `tenant-ledger`: 30 tests plus five deselected targets, `ruff check` and `ruff format --check` clean (no mypy configuration exists in the repository or the example, so none was run). 48 plugin eval cases validated offline, no paid run. `scripts/validate_structure.py`, `claude plugin validate .`, the secrets scan and `git diff --check` pass.

## Compatibility

| Contract | Status |
|---|---|
| CLI, exit codes, options of `audit_fastapi.py` and `audit_diff.py` | unchanged |
| JSON 1.0–1.6 | every key kept; schema 1.7 adds `remediation.recipe`, `remediation.automatable`, `remediation.tool` |
| SARIF 2.1.0 | unchanged structure; the remediation block travels in the existing property bag |
| 59 rules, fingerprints, suppressions, declared contracts, opt-in loop policy, comparison tool | unchanged |
| v1.8 remediation proposal | `status`, `applied`, `verified`, `problem`, `evidence`, `certainty`, `proposed_fix`, `invariants`, `tests`, `regression_risk` kept (asserted) |
| New artefact | `remediation_run/1.1` (JSON), text and markdown renderings; `scripts/audit_remediate.py` with a root shim; options `--trust`, `--trust-scope`, `--executor`, `--link`, `--strict` added to the first delivery's CLI |

## Residual risks and limits

- The local executor is process hygiene, not a sandbox: no network isolation, no filesystem confinement beyond the fresh HOME, no memory limit, and a malicious suite runs with the operator's privileges. The digest check proves the project was not written to by the run, not that the tests did nothing elsewhere. `--trust trusted` is a declaration; use a container for anything you would not run on this machine.
- `inconclusive` depends on reading runner conventions: a custom runner that exits 0 without running anything is read as `passed` unless it prints a pytest or unittest summary; prefer pytest or unittest selectors as reproducers.
- A reproducer linked at rule level is only right when every case of that rule is observed by the same tests; link by file and line otherwise.
- The combined check runs the suite once with every candidate applied; when it fails the candidates keep their individual verdicts and carry the limitation. Attribution of a combined failure is manual.
- Recipes are narrow by design; `move_commit_after_loop` changes failure semantics (all or nothing) and `add_await` makes previously swallowed errors surface; both are documented and verified on PostgreSQL for the shapes tested, not for every shape.
- The environment pass-through is the operator's responsibility: passing a variable that carries a credential defeats the sanitised environment.

## Towards v2.0

An isolated executor (container or microVM) behind the same `Executor` interface so untrusted projects stop failing closed; automatic discovery of reproducers from per-test results; recipes for more rules; an explicit `apply` step that writes to the project only after a recorded human approval; runtime records (`evidence_state: reproduced`) fed back into the audit report.

## Publication recommendation

Local commits only; nothing pushed, no tag, no release. Publish after review of this document, of `references/safe-refactoring.md` and of the executor limits stated above.
