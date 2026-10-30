# FastAPI Architect v1.9 — Verified Remediation & Safe Refactoring

Additive release on top of v1.8.0 (`cffd3bd`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id (59), the suppression markers, the fingerprints and the comparison tool. Report schema 1.7 is a superset of 1.0–1.6: the `remediation` block of a finding gains `recipe`, `automatable` and `tool`. The remediation run is a separate artefact (`remediation_run/1.0`).

## Scope and the brief

The detailed v1.9.0 brief was not available in the implementation session; the work follows the ten points of the request as given (remediation states, reproduction in fixtures, structured patches, execution only in isolated and authorized environments, before/after verification, safe-refactoring recipes, executable examples on PostgreSQL, compatibility with JSON/SARIF and the 59 rules, adversarial and negative controls, documentation and preparation for v2.0), prioritising a complete vertical slice for three rules. Where the brief may have fixed details the request did not repeat (isolation mechanism, artefact format, rule choice), the decisions below are documented and reversible.

| Point | Status | Where |
|---|---|---|
| 1. Remediation model with verifiable states | complete: `proposed`, `blocked`, `patched`, `audit_clean`, `verified`, `rejected`, enforced transitions with history | `fastapi_audit/remediate/model.py` |
| 2. Automated reproduction in fixtures | complete for the three rules: `tests/fixtures/remediation19` (fake session faithful to SQLAlchemy's semantics; three failing tests become five passing) and the PostgreSQL target in `tenant-ledger` | `tests/fixtures/remediation19`, `examples/tenant-ledger/tests/test_remediation_targets.py` |
| 3. Structured patch proposals | complete: line-level edits plus unified diff, original/patched digests, preconditions on the AST | `remediate/patches.py` |
| 4. Execution only in isolated, authorized environments | complete within the stated limits: copy without `.git`/environments, explicit authorization, no shell, allow-list, fresh HOME, environment pass-through, closed stdin, timeout, CPU/file-size limits; no network isolation (documented) | `remediate/isolation.py` |
| 5. Verification before and after by tests and audits | complete: two audits of the copy compared by fingerprint, the test command before and after, `defect_observed_before_patch` | `remediate/pipeline.py` |
| 6. Safe refactoring recipes | complete for the three automatic recipes and two manual ones, with preconditions, invariants, tests and rollback | `references/safe-refactoring.md` |
| 7. Executable examples with FastAPI, SQLAlchemy and PostgreSQL | complete for the begin-block recipe: the tool ran the project's PostgreSQL target inside the copy (before: 1 failed; after: 1 passed; 2 verified) | `examples/tenant-ledger/VERIFICATION.md` |
| 8. Compatibility with JSON, SARIF and the 59 rules | complete: schema 1.7 additive, SARIF unchanged (the remediation block already travels in the property bag), rules untouched | compatibility tests of v1.4–v1.9 |
| 9. Adversarial evaluations and negative controls | complete: 23 tests in `tests/test_audit_v19.py` (plus one opt-in PostgreSQL test) and one new plugin eval case | `tests/test_audit_v19.py`, `evals/remediation-isolated-copy-only` |
| 10. Documentation and preparation for v2.0 | complete: references, README, SKILL.md, this document, the "Towards v2.0" section | `references/safe-refactoring.md`, `references/audit-engine.md` |

## What the tool does

`scripts/audit_remediate.py PROJECT [--select RULE] [--fingerprint FP] [--authorize-execution --test-command CMD] [--patch-dir DIR] [--output run.json]`:

1. audits the project read-only and selects the findings with an automatic recipe (test code excluded unless `--include-tests`);
2. digests the project's Python files;
3. copies the project into a temporary workspace (`.git`, virtual environments, caches and build output excluded) and audits the copy: a finding absent there is `rejected` as not reproduced;
4. with authorization, runs the test command in the copy (**before**);
5. per file from the highest line down, generates and applies each patch in the copy; a failed precondition blocks the case, a file that differs from the audited one rejects it;
6. audits the copy again and compares by fingerprint: a case is rejected when its finding is still detected or new findings appear in its file;
7. with authorization, runs the test command again (**after**);
8. decides: `verified` needs a clean audit and passing tests; without executed tests the best is `audit_clean`; failing tests after the patch reject the case;
9. digests the project again: `original_untouched` must be true or the run fails with an error; writes the `.diff` files when asked.

`applied_to_project` is always `false`. Applying a diff is a human step (`git apply`).

## Recipes (vertical slice)

| Rule | Recipe | Blocked when |
|---|---|---|
| `TX_COMMIT_INSIDE_BEGIN_BLOCK` | delete the `commit()` statement inside `with x.begin()` (`pass` keeps an empty block valid) | the commit shares its line with another statement or is part of a larger expression; the position no longer matches the source |
| `SA_ASYNC_SESSION_NOT_AWAITED` | insert `await` before the call (`await x`, `return await x`, `r = await x`) | the enclosing function is synchronous (manual recipe); the call sits in a lambda or comprehension; already awaited |
| `TX_COMMIT_IN_LOOP` (`loop_kind: per_item`) | delete the per-item commit and insert it after the loop at the loop's indentation (only delete when a commit on the same receiver already follows) | the commit is nested in `if`/`try`/`with` inside the loop; the loop contains `return` or defines a function; the loop is not per-item (deliberate loops, including those reported under the opt-in policy, have no automatic recipe) |

Every patched source must parse; diffs are accepted by `git apply --check` (tested).

## Evidence

- `tests/fixtures/remediation19` on the defective code: 5 tests, 3 fail (atomicity of the per-item import, the begin-block raise, the never-executed statement). Pipeline without authorization: 3 `audit_clean`, nothing executed. With authorization (`python -m unittest`): tests before failed, after passed, 3 `verified`, `defect_observed_before_patch: true`, fixture digest unchanged. The three diffs apply to the untouched fixture with `git apply` and the suite then passes.
- `tenant-ledger` (PostgreSQL 16 on a throwaway cluster, SQLAlchemy 2.0.55): `tests/test_remediation_targets.py` (marker `remediation`, deselected by default) fails on the anti-pattern and passes after the recipe. The tool, with `uv run --frozen --extra dev pytest -q -m remediation ...` authorized inside the copy and `UV_CACHE_DIR`/`UV_PYTHON_INSTALL_DIR` passed through: before 1 failed, after 1 passed, 2 cases `verified` (lines 176 and 190), no new finding, project untouched. A first attempt without `--extra dev` ended with 2 `rejected` because pytest was not in the copy's environment and the suite failed both times: the tool reported the failure instead of claiming anything, which is the intended behaviour.
- Negative and adversarial controls (`tests/test_audit_v19.py`): a command that exits non-zero rejects every patched case; a program outside the allow-list is refused and the cases stay `audit_clean` with the reason; a copy whose file differs from the audited project is rejected; a recipe whose edits change nothing is rejected ("still detected"); findings in test code are skipped unless asked; a project that changes during the run aborts with `RemediationError`; recipe preconditions (sync function, lambda, nested commit, early return, batch loops, policy loops, shared line, moved call, rule without recipe); isolation (fresh empty HOME, no `.git` in the copy, environment variables not passed, cwd is the copy, timeouts reported, unsafe workspace locations refused, allow-list and metacharacter checks); model transitions; CLI contract (exit 2 for a test command without authorization, for rules without recipe and unknown rules; exit 1 with `--fail-on-rejected`; JSON/markdown output; patch directory).
- 327 tests on Python 3.13.5 and 3.9.6 (the PostgreSQL pipeline test is skipped unless `FA_RUN_PG_REMEDIATION=1` with `uv` and PostgreSQL binaries on PATH, and was run once by hand; 5 further skips on 3.9: `jsonschema`); tenant-ledger 30 tests plus the deselected target; `ruff` clean; 48 plugin eval cases validated offline, no paid run.

## Compatibility

| Contract | Status |
|---|---|
| CLI, exit codes, options of `audit_fastapi.py` and `audit_diff.py` | unchanged |
| JSON 1.0–1.6 | every key kept; schema 1.7 adds `remediation.recipe`, `remediation.automatable`, `remediation.tool` |
| SARIF 2.1.0 | unchanged structure; the enriched remediation block travels in the existing property bag |
| 59 rules, fingerprints, suppressions, declared contracts, opt-in loop policy | unchanged |
| New artefact | `remediation_run/1.0` (JSON), text and markdown renderings; `scripts/audit_remediate.py` with a root shim |

## Residual risks and limits

- Isolation is a copy with a sanitised process, not a sandbox: no network isolation, no confinement of writes outside the copy, no memory limit. The digest check proves the project was not written to by the run, not that the tests did nothing elsewhere. Use a container when the project's tests reach the network or external services.
- The test command verifies the suite, not one case at a time: several cases patched in one run share one verdict (use `--fingerprint` to attribute). Per-case workspaces are v2.0 work.
- A project whose suite fails before the patch for unrelated reasons cannot reach `verified` (the after run fails too); the tool says so.
- Recipes are deliberately narrow; everything outside their preconditions is `blocked` with a manual recipe. `move_commit_after_loop` changes failure semantics on purpose (all or nothing) and refuses loops with `return`.
- The environment pass-through is the operator's responsibility: passing a variable that carries a credential defeats the sanitised environment.
- The fake session of the fixture models the semantics the recipes rely on; it is not SQLAlchemy. The PostgreSQL target covers the begin-block recipe only; the other two recipes are verified against the fake session in CI and manually against PostgreSQL only through the invariants named in the recipes.

## Towards v2.0

Per-case workspaces, container-based isolation (network and filesystem), recipes for more rules (offloading a blocking helper, adding a timeout, closing a session), an explicit `apply` step that writes to the project only after a recorded human approval, and runtime records (`evidence_state: reproduced`) fed back into the audit report from a remediation run.

## Publication recommendation

Local commits only; nothing pushed, no tag, no release. Publish after review of this document, of `references/safe-refactoring.md` and of the isolation limits stated above.
