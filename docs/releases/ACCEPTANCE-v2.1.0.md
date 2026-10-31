# FastAPI Architect v2.1.0 — acceptance report (release candidate, 2026-10-31)

Scope: the local commits between the published `d2d50d7` (v2.0.0) and the candidate HEAD, reviewed as one system:
the engineering foundations (increment 0), the isolated container executor (increment 1) and the phased PostgreSQL
migrations with safe backfills (increment 2). Every verdict names its evidence; what was not executed is marked as
such. Nothing was published: no push, tag, release or merge.

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 (`/opt/anaconda3`) and 3.9.6 (`/usr/bin/python3`), PostgreSQL
16.10 (Homebrew) as throwaway clusters under `/tmp`, uv 0.11.28 resolving from the local cache (generated projects
run on CPython 3.13 in their own virtual environment), Ruff 0.17.3 through `uvx`, Docker client 29.9.2 against a
rootful daemon 29.5.2 inside a Colima VM (Ubuntu 24.04.4, kernel 6.8, aarch64, cgroup v2; security options
`apparmor`, `seccomp profile=builtin`, `cgroupns`), image `python:3.13-slim` pinned at
`sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb`. The acceptance module is
`tests/test_acceptance_v21.py`; its reports and timings were kept outside the repository with
`FA_ACCEPTANCE_EVIDENCE`.

Legend of the matrices: **PASS** verified by an executed test or command named in the row; **FAIL** a demonstrated
defect still open; **INCONCLUSIVE** could not be verified in this environment; **NOT APPLICABLE** outside the
capability. For every capability: *recommended* (what the skill advises), *implemented* (what the tool does),
*tested* (how), *not supported* (what it refuses or does not claim).

## 1. Repository integrity

| Check | Verdict | Evidence |
|---|---|---|
| HEAD / remote | PASS | `origin/main` = `d2d50d7ba98e2f75b554b5616f2ef0c865fff7c2`; 17 commits linear, 0 merges, `origin/main` an ancestor of HEAD; author and committer `martinDTV <153138284+martinDTV@users.noreply.github.com>` on every commit; no `Co-Authored-By`, no `Signed-off-by` |
| Working tree | PASS | clean before the acceptance; no stash; every file touched by the range under `plugins/fastapi-architect/`, `docs/`, `tests/`, `scripts/`, `ruff.toml`, `README.md` |
| Foreign work | PASS | consumer repositories untouched; corpora read only; the holdout corpora were not re-scored |
| Versions | PASS | `TOOL_VERSION`, `plugin.json` and `marketplace.json` at 2.0.0 throughout the three increments by decision; bumped to 2.1.0 by the release commit of this acceptance together with the five snapshot manifests and the two tests that pin it; audit `SCHEMA_VERSION` 1.7, 59 rules; `claude plugin validate` passes for the marketplace and the plugin |
| Documentation layout | PASS | root holds `README.md` only; release notes for v1.2–v2.1, `MERGE-NOTES.md`, the v2.0.0 acceptance and this report under `docs/releases/`; decision records and the v2.1 discovery under `docs/architecture/`; guides under `docs/guides/`; `docs/README.md` indexes them; asserted by `scripts/validate_structure.py` |
| Tracked caches or binaries | PASS | none; `.ruff_cache`, `.venv`, `__pycache__` absent from the tree and from the committed snapshots |

## 2. Capability matrix

| Capability | Recommended | Implemented | Tested | Not supported / not claimed | Verdict |
|---|---|---|---|---|---|
| Shared foundations (`fastapi_common`: one version, one transient-artefact set, filesystem helpers, durable atomic write) | internal | yes | unit (`test_foundations_v21.py`), used by every engine | — | PASS |
| Blueprint as a package with byte-identical output | internal | yes | per-file oracle against the v2.0.0 engine on every spec; snapshot tests | — | PASS |
| Durable recovery journal (fsync of file and directory by default; `durable=False` only for verification copies) | yes | yes | fault injection before, during and after publication and during recovery | the drive cache on macOS (`F_FULLFSYNC` not issued), directory entries on Windows | PASS |
| Frozen v2.0.0 artefacts still read with the same meaning (manifests 1.0/1.1, evolution 1.0/1.1, journal 1.0, adoption 1.0, audit report 1.7) | yes | yes | `test_compat_artifacts.py` (9) | — | PASS |
| Isolated container executor: fail-closed selection, non-root, read-only root, no capabilities, no new privileges, seccomp, no network at run time, memory/CPU/PID limits, timeout, labelled resources reaped, evidence bound to the run | yes, the only executor for `untrusted` | yes (Docker CLI runtime; runtime-neutral domain) | unit against a fake runtime (19); real Docker (8): controls asserted from inside, timeout, OOM, pids, refusals, unreachable daemon, leftovers; design, evolution and remediation through the container | the runtime, the daemon, the kernel, the preparation step's egress; Podman and rootless Docker not exercised | PASS |
| Phased migrations: expand revision (nullable column, progress table), concurrent-index revision verified by definition and validity, contract revision online and idempotent, reversible | yes, for PostgreSQL columns that live rows cannot satisfy | yes (contract 1.2, manifest 1.3) | unit (18); real PostgreSQL (`test_migrations_v21_postgresql.py` 4, acceptance 3) | renames, drops, type changes, multi-column or partial indexes, arbitrary data migrations | PASS |
| Generated backfill runner: keyset batches, `FOR UPDATE SKIP LOCKED`, checkpoint with the rows in one transaction, resumable, one worker per operation, checksum-guarded checkpoint, RLS refusal, `complete` after a zero count, reopen for rows that appear later, evidence writer | yes | yes | real PostgreSQL: locked rows, SIGKILL, two workers, concurrent writes, reopen, corrupt and foreign checkpoints, connection failure, time limit, RLS | backfills as the application role under RLS; primary keys other than the generated uuid `id`; tables beyond a few thousand rows not measured | PASS |
| Contract phase gated by a human (`--authorize-contract`, `declared_by`), fresh evidence and twelve named preconditions; re-check on the live database by the revision | yes | yes | real PostgreSQL: each precondition blocked in turn; outdated-but-fresh evidence accepted by the plan and refused by the revision | the deployed application version is a declaration, never demonstrated by the tool | PASS |
| Evolution contracts 1.0, 1.1 and design contracts 1.0–1.2 keep their meaning | yes | yes | compat artefacts, evolution suites (66), design suites (105), examples | — | PASS |
| Auditor (59 rules, JSON, SARIF, fingerprints, suppressions, declared contracts) | yes | unchanged | full suites; precision harness against `d2d50d7` (section 6) | — | PASS |
| CLI surface of v2.0 (`design_fastapi.py`, `evolve_fastapi.py`, `audit_fastapi.py`, `audit_remediate.py`) | yes | unchanged plus `--executor`/`--container-*`, `--authorize-contract`, `--evidence` | CLI tests in the suites | — | PASS |
| Database inside the container executor (sidecar) | deferred | no | — | not provided: database-backed suites run through the local executor | NOT APPLICABLE |
| Renames with dual-write, three-way merge of hand edits | deferred | no | — | refused as `unsupported` by the contracts | NOT APPLICABLE |

## 3. Backfills with locked rows (`test_01_locked_rows_are_skipped_now_filled_later_and_never_omitted`)

600 live rows; the row with the lowest key and the row at position 300 held under `SELECT ... FOR UPDATE` by two
other sessions for the whole run.

| Step | Result |
|---|---|
| Run with both rows locked (`--batch-size 100`) | exit 6 `incomplete`; 598 rows in 6 batches and 3 passes; `pending_rows` 2; the checkpoint moved past the locked first key (keys above it were processed); both locked rows still NULL; 0.44 s wall for the command |
| First lock released, run again | the row with the lowest key, far below the checkpoint, is found by the pass that restarts from the beginning: 1 row, exit 6 (the middle row still locked), `pending_rows` 1 |
| Second lock released, run again | `complete`; 1 row; `rows_processed` 600; passes accumulate across the three runs; `finished_at` set; same operation id |
| Rows already filled | a further run processes 0 rows (`already complete: zero pending rows re-counted`); the md5 of every `(id, carrier)` pair is unchanged |
| Rows that appear after `complete` | 5 inserted; the next run re-counts, reopens (`5 rows appeared after completion: reopened`), fills exactly 5; `rows_processed` 605, batches and passes increased, the 600 original values unchanged; the evidence is `ok` with the cumulative record |
| SIGKILL mid-run, resumption | scenario B: killed at 300 processed rows, checkpoint == rows filled, resumption fills the remaining 700 in 4 batches (`rows_processed` 1 000, every row once) |
| Two competing workers | `test_migrations_v21_postgresql.py`: exit codes `[0, 3]`, exactly one progressed |
| Checkpoint and data in one transaction | the kill test (checkpoint equal to rows filled) and the runner's `_batch` (select, update, checkpoint under one `conn.begin()`) |

Verdict: PASS. A locked row is skipped in the pass that meets it and found by a later pass that restarts from the
beginning after the count; nothing is omitted for good. The limit stays: while a row is locked the operation is
`incomplete`, never `complete`, and the runner says so with the count.

## 4. Semantics of `complete` and reopening

- `complete` = zero pending rows counted after the last pass of a run. It is a measurement at a time, recorded with
  `finished_at`.
- A later `run` on a `complete` operation re-counts first; zero means nothing happens (`rows_this_run` 0, the record
  untouched); otherwise the operation is reopened from the start **in the same record**: same `op_id`, `state`
  back to `running`, `last_key` reset, `rows_processed`, `batches` and `passes` keep accumulating, `finished_at` set
  again at the end. History, identity and evidence are preserved: the evidence shows the cumulative counters and
  the checksum of the definition is unchanged.
- The contract revision records an empty table it finds in a fresh environment with the sentinel checksum
  `empty-table-at-contract`; the runner and the evidence recognise it (a run has nothing to do; `verify` is `ok`).

## 5. Contract evidence (`test_02_contract_preconditions_separate_declaration_observation_and_automation`)

The twelve preconditions of a contract plan, by what they rest on:

| Rests on | Preconditions |
|---|---|
| A declaration by the operator (recorded as such, never demonstrated by the tool) | `contract_authorized` (`--authorize-contract` and `declared_by`), `contract_application_version_accredited` (the version the operator declares as the one that writes the column) |
| The state observed in the database, carried by the evidence | `contract_database_at_head`, `contract_column_present`, `contract_backfill_complete`, `contract_no_pending_rows`, `contract_indexes_valid` |
| Automated checks on documents | `contract_evidence_present`, `contract_evidence_valid`, `contract_evidence_fresh`, `contract_evidence_matches`, `contract_operation_pending`, `contract_reversible` |

Blocked on the real database, each in turn: a NULL that reappeared (`contract_no_pending_rows`; a re-run reopened
the operation and filled it); evidence 30 h old (`contract_evidence_fresh`); the database one revision below the
manifest head after `alembic downgrade -1` (`contract_database_at_head`, with the index reported *missing*); an
unexpected revision recorded in `alembic_version` (`contract_database_at_head`); an index flagged `indisvalid =
false` (`contract_indexes_valid`).

Repeated just before the DDL: evidence that was true when taken (a NULL set afterwards) is accepted by the plan,
the evolution is verified in a copy and written, and the contract revision refuses on the live database
(`1 row(s) still NULL`); the column stays nullable; `verify` then reports `declared contracted but the column is
still nullable`. With the row fixed the revision applies: both columns `NOT NULL`, no transitional constraint left.

The race between the last count and the DDL, demonstrated with the statements the revision uses: after
`ADD CONSTRAINT ... CHECK (c IS NOT NULL) NOT VALID` an insert with NULL is rejected (SQLSTATE 23514), so no NULL
enters after that point; a NULL inserted between the count and the constraint makes `VALIDATE CONSTRAINT` fail and
the column stays nullable; after the fix `VALIDATE` and `SET NOT NULL` succeed. What is protected
transactionally: every step is its own statement and PostgreSQL's constraint machinery closes the window. What
depends on deployment discipline: an application version that still inserts NULLs receives constraint errors from
the moment the constraint exists; the tool cannot prove such a version is gone, it records the operator's
declaration. Not re-checked by the revision: index validity (a plan-time check from the evidence) and the declared
version.

Verdict: PASS. Online compatibility is **not** declared on the strength of the version declaration; it rests on
the re-checks above and on the constraint semantics, with the residual stated.

## 6. Docker executor (`FA_CONTAINER=1 tests/test_container_v21.py`, real daemon)

| Control | Verdict | Evidence |
|---|---|---|
| Fail-closed selection | PASS | `select_executor` never falls back; the local executor still refuses `untrusted`; three refusals (image outside the allow-list, `network_run: bridge`, a digest that does not exist) create nothing |
| Non-root user, read-only root, capabilities dropped, no new privileges, seccomp | PASS | asserted from inside the container by the fixture (`uid 65534`, root read-only, `CapBnd` 0, `NoNewPrivs` 1, seccomp mode 2) and from the daemon view, consistent |
| Network | PASS | only `lo`, `ENETUNREACH`, no socket, no credentials, no secret-looking keys at run time; the preparation step uses `bridge` by design (recorded) |
| Memory, CPU, PID limits, timeout | PASS | `memory.max` 536870912 and `pids.max` 256 read inside; a 128 MiB limit ends a 1 GiB allocation without a pass; a pids limit of 32 bounds process creation; a 15 s timeout kills a 120 s sleep and reports `timeout` |
| Cleanup | PASS | labelled leftovers of an interrupted run are reaped; no containers left after the suite (`docker ps -a` empty of the label) |
| Evidence (pytest/unittest) | PASS | nonce-bound report read from the mounted workspace; `environment` record with the restrictions in force and `not_provided` |
| Integration with design, evolution and remediation | PASS | design of `inventory-service` as `untrusted`, evolution as `fixture`, remediation of `remediation19` as `untrusted`, all `verified` through the container |
| Not verified here | INCONCLUSIVE | Podman; rootless Docker; Linux hosts with native bind-mount permissions; the VM boundary of Colima itself; kernel vulnerabilities |

27 tests OK in 41.5 s (8 against the daemon), 33 MB host RSS.

## 7. PostgreSQL results

| Module | Result |
|---|---|
| `tests/test_migrations_v21_postgresql.py` (increment 2 slice, index conflicts, connection failure and time limit, RLS) | 4 OK in 61.5 s, 130 MB in the final chain (42.2 s in the increment-2 run whose numbers are in `examples/generated/VERIFICATION.md`) |
| `tests/test_acceptance_v21.py` PostgreSQL part (locked rows, contract preconditions, scenario B) | 3 OK; whole module 4 OK in 100.4 s, 124 MB in the final chain (68.5 s in an earlier run of the day) |
| `tests/test_design_v20_postgresql.py` (generated backends of v2.0, restricted role) | 9 OK in 66.0 s, 132 MB |
| `tests/test_acceptance_v20.py` with `FA_DESIGN_PG=1` | 3 OK in 36.4 s |

## 8. End-to-end scenarios (`tests/test_acceptance_v21.py`)

**Scenario A, generated and evolved backend (in memory, no database).** `inventory-service`: contract loaded, no
generation refusal, `layered` recommended deterministically; generated twice into two directories with identical
file digests and identical manifests (no timestamps); generated once more with audit (info findings at most) and
its 10 tests passed through the trust model (nonce matched, verifiable, manifest `identical`); a hand-written
`custom_rules.py` outside the manifest and a hand edit on `services/catalog.py` (listed under `files.preserve`);
the published `inventory-add-suppliers.json` planned (`planned`, no conflicts), verified in a copy (20 tests passed,
routes 6 → 11, no route missing, no protected file changed) and applied: create 7, modify 4, unchanged 24,
`preserved_manual_changes` 1; both protected files byte-identical afterwards; discovery reports the custom file
`unmanaged`, the edited one `managed_modified`, every other file `managed_unchanged`; history `generate`, `evolve`;
the same contract again is `noop` and changes no byte. 9.6 s wall in the final chain (2.3 s in an earlier run).

**Scenario B, phased PostgreSQL migration.** `parcels` with 1 000 live rows: expand evolution verified (18
generated tests) and applied in 28.4 s in the final chain (15.1 s in an earlier run); expand and concurrent-index revisions upgraded, index valid; the runner
killed by SIGKILL at 300 processed rows with the checkpoint equal to the rows filled; resumption to `complete` in
0.44 s (700 rows, 4 batches, 1 000 processed exactly once, one distinct value); the second operation in 4 batches;
7 new rows after `complete` reopened both operations for exactly 7; evidence `ok` (both operations at 1 007,
index valid); the authorised contract evolution verified (14 passed, 4 skipped by design) and applied in 11.0 s;
the contract revision applied in 0.51 s: both columns `NOT NULL`, the md5 of every `(id, carrier, tracking_token)`
unchanged by the contract, 1 007 rows, no transitional constraint, one head, final evidence `ok`, manifest phases
`initial, expand, index, contract`, both changes declared `contract`, manifest `identical`, 4 revisions with no
historical file modified, an insert without `carrier` rejected. 52.9 s wall in the final chain (31.1 s in an earlier run).

## 9. Compatibility

| Area | Verdict | Evidence |
|---|---|---|
| 59 rules, JSON and SARIF, fingerprints, suppressions | PASS | full suites (`test_sarif_schema.py`, audit suites v1.3–v2.0); the auditor's code unchanged since increment 1 (import order and executor wiring only since `d2d50d7`) |
| Auditor precision against `d2d50d7` | PASS | `scripts/audit_precision.py --against d2d50d7` on truth16 (16 tp / 0 fp / 0 fn / 23 tn), truth17 dev (precision 1.0, recall 1.0) and acceptance (0.786 / 0.846, the same two false negatives and three false positives as v2.0.0), truth18 dev and acceptance (1.0 / 1.0): 53 per-rule rows compared, none differ; regression fixtures diffed between the two engines +0 / −0 (`named_authz` 23, `named_authz_privilege` 2, `sa_sessions` 18, `transactions` 9, `tenancy_unprotected` 4); call-resolution coverage and evidence counts identical; holdout not touched |
| Design contracts 1.0–1.2 | PASS | design suites (105 tests) and the five examples |
| Evolution 1.0–1.2 | PASS | evolution suites (66), the compat artefacts (1.0, 1.1 contracts), the eight example contracts (1.0, 1.1 and the two 1.2 of `parcels`) |
| Manifests 1.0–1.3 | PASS | compat fixtures (1.0 project with its spec, 1.1 project), the four regenerated 1.3 snapshots, discovery of all of them |
| Adoption 1.0 | PASS | `test_evolve_v20_adoption.py`, compat fixture |
| Remediation run 1.1 → 1.3 | PASS | `test_container_v21.py` schema test (additive), remediation suites |
| Journal 1.0 and recovery | PASS | `test_evolve_v20_recovery.py`, compat fixture of an interrupted journal, fsync fault injection |
| CLI of v2.0 | PASS | every flag and exit code kept; new flags additive |
| Python 3.9 and 3.13 | PASS | full suites on both (section 10); byte-compilation of every new module on 3.9 |
| Claude Code installation and discovery | PASS | `claude plugin validate` on the marketplace and on the plugin; `claude plugin list` discovers `fastapi-architect` (the copy installed on this machine is an older 1.3.0 install and was not reinstalled by this acceptance) |
| Holdout corpora | NOT APPLICABLE | not re-scored, by rule |

## 10. Tests and validations executed

| Validation | Result |
|---|---|
| Full suite, Python 3.13.5 | 629 tests OK, 26 skipped (the 19 of increment 1, the 4 PostgreSQL phased tests and the 3 PostgreSQL acceptance tests when `FA_DESIGN_PG` is unset); 128.9 s wall, 139 MB max RSS in the final chain (the same suite took 86.3 s earlier in the day; the figure varies with machine load) |
| Full suite, Python 3.9.6 | 629 tests OK, 41 skipped (plus the generated suites and scenario A, which need Python 3.12+); 138.0 s wall, 182 MB max RSS |
| Real PostgreSQL (four modules) | section 7 |
| Real Docker | section 6 |
| Ruff (committed configuration, plugin code and tests) | clean |
| `scripts/validate_structure.py` | OK |
| `claude plugin validate` (marketplace and plugin) | passed |
| `tests/test_docs_links.py` | OK |
| Offline evals | 48 cases structurally validated (`test_plugin_evals.py`); no paid run |
| JSON/SARIF | `test_sarif_schema.py` and the compat artefacts |
| Secrets scan on the diff of the range and every new file | none; placeholders only (`USER:PASSWORD@localhost` in generated READMEs) |
| `git diff --check` | clean |

## 11. Defects found by this acceptance

| Defect | Severity | Status |
|---|---|---|
| A concurrent index the database does not hold was reported as *invalid* instead of *missing* by `contract_indexes_valid` (the plan blocked either way; the diagnosis was wrong) | non-blocking (wrong wording in a blocker) | fixed in `migrations.py`; regression in the unit module and in the acceptance module |
| Defects found while increment 2 was being verified on the real cluster (six in the generated runner and its tests) | were blocking | fixed before this acceptance; listed in `examples/generated/VERIFICATION.md` |

No blocking defect was open at the end of the acceptance.

## 12. Residual risks

- **Docker and the daemon.** The executor hardens the container; it does not protect against the runtime, the
  daemon, the kernel or a compromised image registry. On this machine the daemon is rootful inside a VM; rootless
  Docker and Podman were not exercised. An operator who authorises `untrusted` code accepts the runtime's boundary.
- **Preparation with network.** Dependencies are resolved in a container with `bridge` network and no host
  credentials; that step has egress by design and is recorded as such. Supply-chain trust in the resolved packages
  is the operator's.
- **Backfills with concurrent writes.** Guaranteed: never overwriting a value, never waiting for the application,
  `complete` after a zero count, reopening for rows that appear later. Not guaranteed: that an application version
  that does not write the column stops inserting NULLs; the contract revision refuses them and the constraint
  rejects them afterwards, which is an error for that version, not silent corruption.
- **Limits of `SKIP LOCKED`.** A locked row is skipped in the pass that meets it; the run ends `incomplete` with the
  count if it stays locked through three passes. Long-held locks mean repeated runs, never a false `complete`.
- **Evidence of deployed versions.** `contract_application_version_accredited` is a declaration by the operator; the
  tool records it and cannot verify what is deployed.
- **Scale.** The largest table exercised holds 2 540 rows; the batch loop is bounded per step by construction and the
  final count is a scan of the NULL rows; no measurement beyond that is claimed.
- **RLS isolation.** Backfills on RLS tables run only as a role that bypasses RLS (the owner); per-tenant backfills
  as the application role are not provided. The throwaway owner is a superuser; a non-superuser owner in production
  is subject to `FORCE` and needs the context or `BYPASSRLS`.
- **Journal durability.** fsync of file and directory on a filesystem that honours it; not the drive cache on macOS,
  not directory entries on Windows. A failed sync is an error, never a silent downgrade.

## 13. Documentation state

| Document | State |
|---|---|
| `docs/releases/RELEASE-NOTES-v2.1.md` | three increment sections with measured numbers; status set to release candidate by the release commit |
| `docs/architecture/discovery-v2.1.md` | decisions of section 10; status line names the implemented increments |
| `docs/architecture/adr-2.1-01-phased-migrations.md`, `adr-2.1-03-isolated-container-executor.md` | accepted, implemented |
| `docs/guides/phased-migrations-and-backfills.md`, `isolated-execution-with-containers.md` | written against the executed scenarios |
| `references/design-contracts.md`, `evolution-contracts.md`, `safe-refactoring.md`, `SKILL.md` | contract 1.2, manifest 1.3, report 1.3, the container executor and the phased flow documented; indexed by `docs/README.md` |
| `examples/generated/README.md`, `VERIFICATION.md` | five examples; the increment-2 verification with every number |
| `README.md` | v2.1 section added by the release commit; the v2.0 limit on expand/contract points at v2.1 |

## 14. Verdict

**READY** as a release candidate, pending the author's authorisation to publish. The three increments were
reviewed as one system; the only defect this acceptance found was a wrong diagnosis in a blocker message and it is
fixed and covered; every guarantee stated in the documentation matches an executed test on this machine, and every
limit (runtime boundary, preparation egress, concurrent writers, `SKIP LOCKED`, declared versions, scale, RLS,
journal durability) is written where the user will read it. Nothing was pushed, tagged, merged or released.
