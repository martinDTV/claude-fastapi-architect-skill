# Safe refactoring recipes and verified remediation (v1.9)

The auditor reports; `scripts/audit_remediate.py` proposes, applies **in an isolated copy**, and verifies. It never writes to the audited project: the output is a state per finding and a unified diff a human applies with `git apply`. This document is the contract behind each state and the manual recipe behind each automatic one.

## States, and what each one may claim

| State | Claim | Required evidence |
|---|---|---|
| `proposed` | a recipe exists for the finding | the audit report (`remediation.recipe`) |
| `blocked` | no safe automatic patch | a failed precondition, named in `reason`; the manual recipe below applies |
| `patched` | the patch applied in the copy; verification incomplete | intermediate only |
| `audit_clean` | after the patch the finding is **no longer detected** in the copy and **no new finding** appeared | two audits of the copy compared by fingerprint |
| `verified` | `audit_clean` **and** the authorized test command passed in the copy after the patch | the command's exit status, recorded with its output tail; `defect_observed_before_patch` says whether the same command failed before |
| `rejected` | the patch did not apply, broke parsing, left the finding, introduced findings, or the tests failed after it | `reason` |

Never claimed: that the project is fixed. `applied_to_project` is always `false`; `original_untouched` is computed from a digest of the project's Python files before and after the run and a difference aborts the run with an error. The test command verifies the **suite**, not one case at a time: when several cases are patched in one run, a failing suite rejects all of them (run them one by one with `--fingerprint` to attribute).

## Isolation: guarantees and limits

Guaranteed and tested: the copy excludes `.git`, virtual environments, caches and build output; nothing executes without `--authorize-execution`; the command is split without a shell and refused when it contains shell metacharacters or when its program is not an interpreter (`python`, `python3`, `python3.x`), `pytest` or `uv` (extend with `--allow-command`); it runs with the copy as working directory, a fresh `HOME` inside the workspace, only the pass-through variables (`PATH`, locale, `TMPDIR`; add names with `--env-passthrough`, for example `UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR` for `uv`), stdin closed, a wall-clock timeout, and CPU-time and file-size limits on POSIX.

Not guaranteed: network isolation, confinement of writes outside the copy, memory limits. A project whose tests reach the network or write outside their tree must be remediated inside a container; the run report says so (`isolation.network`).

## Recipes

Each recipe states preconditions (checked on the AST; a failure blocks the case), the change, the invariants that must hold, the tests that demonstrate them, and the rollback (always: discard the copy; nothing happened to the project).

### `remove_commit_inside_begin` — `TX_COMMIT_INSIDE_BEGIN_BLOCK`

- Preconditions: the `commit()` is an expression statement alone on its line inside `with x.begin()` / `async with x.begin()`; the file parses before and after.
- Change: delete the statement (replace by `pass` when it was the only statement of its block).
- Invariants: every statement of the block runs inside the transaction the block owns; all writes of the block are visible together after exit and absent together after an exception; no `InvalidRequestError` on the statement that followed the commit.
- Tests: the block's writes are visible from a second session after exit; raising inside the block leaves none of them; on SQLAlchemy 2.0 with PostgreSQL the statement after the former commit no longer raises (`examples/tenant-ledger/tests/test_remediation_targets.py`).
- Manual variant: if the early commit was intended, drop the `begin()` block and manage the transaction explicitly.

### `add_await` — `SA_ASYNC_SESSION_NOT_AWAITED`

- Preconditions: the call is inside an `async def` (not a lambda or comprehension), not already awaited; the file parses after inserting `await`.
- Change: `session.execute(...)` → `await session.execute(...)` (also as `return await ...` and `x = await ...`).
- Invariants: the statement executes exactly once and its result is used; no `coroutine was never awaited` warning.
- Tests: run under `-W error::RuntimeWarning`; assert the statement's effect from a second session.
- Manual variant (blocked case): a synchronous function must become `async def` and be awaited up the chain, or use a synchronous `Session`.

### `move_commit_after_loop` — `TX_COMMIT_IN_LOOP` with `loop_kind: per_item`

- Preconditions: the commit is a direct child of the loop body, alone on its line; the loop contains no `return` and defines no function; the loop reported by the audit matches the source. Deliberate loops (`batch`, `paginated`, `convergence`, and loops reported only under the opt-in policy) have **no** automatic recipe: changing their transaction boundary is a design decision.
- Change: delete the per-item commit and insert the same statement after the loop at the loop's indentation (only delete when a commit on the same receiver already follows the loop).
- Invariants: either every item is committed or none is; the number of committed rows after a successful run is unchanged; a failure half-way leaves nothing committed.
- Tests: make a later iteration fail and assert nothing is committed; count transactions (one).
- Manual variant: when partial progress is wanted, batch explicitly under a visible size test and make the per-item write idempotent (then the loop is `batch` and the auditor is silent).

### Manual-only recipes (reported with `automatable: false`)

- `ASYNC_BLOCKING_CALL_INDIRECT`: offload the helper (`anyio.to_thread.run_sync`), make the chain async, or declare the route with plain `def`; test concurrent latency.
- `SEC_RESOURCE_AUTHZ_UNVERIFIED`: load the resource through a dependency that receives the principal, or scope the repository call by the principal; test as an unrelated user.

## Using the tool

```
python3 scripts/audit_remediate.py PROJECT --select TX_COMMIT_INSIDE_BEGIN_BLOCK            # static: audit_clean at best
python3 scripts/audit_remediate.py PROJECT --authorize-execution --test-command "pytest -q" --patch-dir patches --output run.json
git apply patches/TX_COMMIT_INSIDE_BEGIN_BLOCK-<fingerprint>.diff                             # the human step
```

`--format json` prints the run report (`remediation_run/1.0`), `--keep-workspace` leaves the copy for inspection, `--fail-on-rejected` gates CI. The audit report's `remediation` block names the recipe (`recipe`, `automatable`, `tool`) so a consumer knows which findings the tool can act on.

## Towards v2.0

Per-case workspaces (attribute test results to one patch), container-based isolation (network and filesystem), recipes for more rules (offloading a blocking helper, adding a timeout, closing a session), an explicit `apply` step that writes to the project only after a recorded human approval, and runtime records (`evidence_state: reproduced`) fed back into the audit report.
