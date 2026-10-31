# FastAPI Architect v2.1.0 — Safe Change at Scale (in progress)

Status: in progress on top of the published v2.0.0 (`d2d50d7`). The scope was decided on 2026-10-31 after the
discovery recorded in [`docs/architecture/discovery-v2.1.md`](../architecture/discovery-v2.1.md) (section 10):
increments 0, 1, 2 and 6. This document grows one section per increment. The version constant, the plugin manifests
and the generated manifests stay at 2.0.0 until the release increment bumps them.

## Increment 0: Engineering Foundations & Technical Debt

Nothing a user generates, audits or evolves changes in this increment: the generated output of every known contract
and example evolution is byte-identical to v2.0.0 (nine-entry oracle), the audit of the four committed examples is
identical, the auditor's precision per rule is identical on the corpora and on the two calibration projects, and
every v2.0 test is still present and green. What changes is the engineering underneath.

### Shared infrastructure: `fastapi_common`

A third package next to `fastapi_audit` and `fastapi_design`, standard library only, imported by both and importing
neither:

- `version.py`: `TOOL_VERSION`, declared once; `fastapi_audit.model.TOOL_VERSION` re-exports it and
  `scripts/validate_structure.py` checks it against the plugin manifests;
- `artefacts.py`: the transient artefacts (`TRANSIENT_DIRS`: version control, `__pycache__`, the Ruff, pytest, mypy
  and Hypothesis caches, tox/nox, build output, `node_modules`; `VIRTUALENV_DIRS`; `TRANSIENT_FILES`: `.DS_Store`,
  `Thumbs.db`, `desktop.ini`; bytecode suffixes) with `walk_files` / `relative_files` / `is_transient`. Deliberately
  not transient: configuration directories (`.vscode`, `.idea`, `.github`), a directory named `env` for the audit (a
  plausible package name; the workspace copy keeps excluding it as in v1.9) and `*.fa-tmp` files (evidence of an
  interrupted write);
- `fs.py`: `sha256_bytes` / `sha256_text` / `sha256_path`, `symlink_on_path`, `resolve_against` (contract-relative
  paths) and `atomic_write(durable=...)`.

Consumers: the audit file walker (`DEFAULT_EXCLUDED_DIRS` = the shared set + the virtual environments +
`site-packages`), the verification workspace (`DEFAULT_IGNORED_DIRS` = the shared set + the virtual environments +
`env`), the design engine (digests, symlink checks, path resolution), and every test that compares project trees.

### The `.ruff_cache` incident, fixed at the root

During the v2.0.0 publication a read-only `ruff check` inside the committed examples left `.ruff_cache/` directories
and the snapshot test failed reading them as UTF-8. The causes: sixteen test sites walked trees with `rglob("*")`
and the snapshot comparison excluded only `__pycache__`; the repository `.gitignore` listed none of the tool caches
(they looked ignored only because Ruff writes its own `.gitignore` inside its cache). Now the comparisons use
`walk_files`, which prunes the shared transient set, `.gitignore` lists `.ruff_cache/`, `.pytest_cache/`,
`.mypy_cache/`, `.hypothesis/` and `Thumbs.db`, and a test plants a Ruff cache, a `.DS_Store` and bytecode inside a
copy of a snapshot and proves the comparison and `verify_manifest` ignore them while every file read is text.

### Duplicated helpers extracted

`_no_symlink_on_path` (generator and journal), `_resolve_project` (adoption and evolution) and four digest helpers
(design discovery, generator, journal, remediation patches and pipeline) now delegate to `fastapi_common.fs`; the
original names stay as thin wrappers or aliases, so nothing that imported them changed.

### `blueprint.py` is a package

The 3 552-line module became `fastapi_design/blueprint/` with nine modules, code moved verbatim: `_base` (type
tables, constants, `Lineage`, `Blueprint`, `Layout`), `_helpers`, `scaffold`, `security`, `persistence`, `modules`,
`testing`, `security_tests` and `__init__` (`build` and the public names: `Blueprint`, `Layout`, `Lineage`, `build`,
`initial_revision`, `delta_revision`, `added_columns`, `migration_file`). The split was done by a script that
assigned every top-level node to a module, derived each module's imports from the names it references and refused
any import cycle; the oracle (every spec under `examples/specs` and `tests/fixtures/design20/specs`, every example
evolution planned on a fresh generation) and the four committed snapshots proved the output byte-identical before
and after. Increment 2 adds its migration generators to `persistence` instead of to a four-thousand-line file.

### Lint configuration for the plugin's own code

`ruff.toml` at the repository root: `target-version = "py39"` (the supported floor), rules `E4, E7, E9, F, I, W`,
fixtures, examples, evals and docs excluded (they are corpora or carry their own configuration), no pyupgrade rules
(so `typing.List` / `Optional`, which the 3.9 interpreter needs, are never rewritten), no `E501` (long single-line
dictionaries are a deliberate style), no bugbear in this increment. The real findings fixed: an annotation in
`fastapi_audit/kinds.py` named `Tuple` without importing it (latent, harmless under postponed annotations), 36
unused imports, 24 import blocks out of order, nine single-letter `l` variables, four mid-file imports in
`rules/reliability.py` moved to the top (no cycle: `transactions` does not import `reliability`), two dead locals
in the blueprint, two `f`-strings without placeholders, one-statement-per-line in `validate_structure.py`, and the
names the auditor package imports for re-export listed in its `__all__`. The skill entry point keeps its re-export
block under a documented per-file ignore. A test runs Ruff with the committed configuration when it is available.

### Durable journal

Every write of the recovery protocol (staged content, backups, each journal state, each landed file, each restored
file, the cleanup, the directories that hold a new operation) goes through `atomic_write(durable=True)`: the file is
fsync'ed before the rename and the directory entry after it. Once `apply` or `recover` returns, its effects survive a
crash or a power loss on a filesystem that honours `fsync`. Not guaranteed, and written where the user reads it
(`references/evolution-contracts.md`, the evolution guide, the journal docstring): the drive's own cache on macOS
(`F_FULLFSYNC` is not issued), directory entries on Windows, filesystems that refuse to sync a directory (the file is
synced, the entry is not, nothing fails). A failed sync of a file is an error, never a silent downgrade: `prepare`
reports `could not stage` and removes the directories it created; `publish` leaves a journal in `publishing` that
`recover` finishes or undoes. The verification copy is written with `durable=False`: a throwaway tree gains nothing
from fsync. Tests: sync order (file, rename, directory), no sync when not durable, file-sync failure leaves the
destination and no temporary, directory-sync refusal reported not raised, the journal syncs by default and not for a
copy, staging failure leaves nothing, publication failure leaves a recoverable journal.

### Compatibility fixtures

`tests/fixtures/compat/` freezes artefacts written by v2.0.0 (provenance in its README): a project with a manifest
1.0 extracted from the increment-1 commit together with the contract it was generated from, a project with a manifest
1.1 from the increment-4 commit, evolution contracts 1.0 and 1.1, an operation journal 1.0 interrupted during
publication, an adoption baseline 1.0 and an audit report 1.7 with its fingerprints. `tests/test_compat_artifacts.py`
proves: the 1.0 project is discovered with every file managed and blocks evolution without its contract, plans with
it and refuses a stale one; the 1.1 project plans and verifies in a copy; both contracts plan against the frozen
project and 1.0 still rejects `modify_modules`; the interrupted journal is classified and both recovery strategies
apply; the adoption baseline marks its files `adopted_unchanged`; the current auditor reproduces every frozen
fingerprint and the comparison reports nothing new and nothing gone.

### Deliberate differences from v2.0.0

- Journal writes fsync by default (see above); `prepare` removes the `ops` and `.fastapi-architect` directories it
  created when staging fails (v2.0 could leave an empty `ops` directory behind).
- The audit excludes `.hypothesis/` directories and the workspace copy skips them too; no corpus or calibration
  project contains one (precision identical).
- `DEFAULT_IGNORED_DIRS` is derived from the shared set (same names plus `.hypothesis`, different order).
- Tree comparisons in the test suite ignore transient artefacts.
- Unchanged: generated output, audit findings, rule identifiers, fingerprints, every schema, every CLI flag and exit
  code, the guarantees fixtures.

### Validation

| Check | Result |
|---|---|
| Byte-level oracle: every spec under `examples/specs` and `tests/fixtures/design20/specs` built, every example evolution planned on a fresh generation (9 entries) | identical before and after every step (helper extraction, blueprint split, lint fixes) |
| Audit of the four committed examples (findings, contracts, files scanned) | identical to v2.0.0 |
| Full suite, Python 3.13.5 | 576 tests OK, 11 skipped (25 new); 107.5 s wall, 136.5 MB max RSS (v2.0.0: 551 tests, 109.4 s, 138.9 MB on the same machine) |
| Full suite, Python 3.9.6 | 576 tests OK, 24 skipped; 109.7 s, 185.2 MB (v2.0.0: 551, 110.9 s, 177.1 MB) |
| Opt-in PostgreSQL module (`FA_DESIGN_PG=1`, PostgreSQL 16.10 throwaway clusters, restricted role) | 9 OK, 117.1 s, 130 MB |
| Opt-in PostgreSQL acceptance scenario (`tests/test_acceptance_v20.py`) | 3 OK, 56.8 s, 135 MB |
| Auditor precision against `d2d50d7` (`scripts/audit_precision.py` on truth16, truth17 dev and acceptance, truth18 dev and acceptance) | tp/fp/fn/tn identical per rule; regression fixtures diffed between the two versions: +0 / −0 (named_authz 23, named_authz_privilege 2, sa_sessions 18, transactions 9, tenancy_unprotected 4) |
| Calibration projects, read-only (`--root`, then the two auditors back to back) | AgentForge 15 → 15 findings, 288 files; Clivana 50 → 50, 467 files, identical fingerprints and identical interprocedural counts on an idle machine (a first run under CPU contention had truncated the published auditor's interprocedural pass at its 5 s budget: environmental, not behavioural) |
| Ruff with the committed configuration | clean on the engines, the root scripts and the tests (84 findings fixed, none suppressed except the documented entry-point re-exports) |
| `scripts/validate_structure.py`, `claude plugin validate` (plugin and marketplace), `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff, byte-compilation of every module with Python 3.9 | pass |
| Holdout corpora (truth17/holdout, truth18/holdout) | not re-scored |

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 (`/opt/anaconda3`) and 3.9.6 (`/usr/bin/python3`), PostgreSQL 16.10 (Homebrew), uv 0.11.28, Ruff 0.17.3 through `uvx`.

## Increment 1: Isolated Container Executor

Untrusted code can now be verified. `--executor container` on `design_fastapi.py`, `evolve_fastapi.py` and
`audit_remediate.py` runs the project's tests inside an OCI container (Docker first, through its CLI; the domain is
runtime-neutral and a Podman adapter fits behind the same `ContainerRuntime` interface) and is the only executor
that accepts the `untrusted` profile. The local executor keeps the `fixture` and `trusted` profiles with its
documented limits. There is no fallback between them: without a usable daemon an untrusted project stays
unverified. Decision record: `docs/architecture/adr-2.1-03-isolated-container-executor.md`; guide:
`docs/guides/isolated-execution-with-containers.md`; contract: `references/safe-refactoring.md`.

### Design

- `fastapi_audit/remediate/containers.py`: `SandboxPolicy` and `ImagePolicy` (every field a restriction; the
  mandatory ones cannot be weakened and a policy that tries is refused), `ContainerSpec`/`Mount` (runtime-neutral),
  `ContainerRuntime` (the adapter interface) and `DockerCliRuntime` (argv lists, no shell, a minimal client
  environment), `verify_effective` (the daemon's view of a created container against the specification),
  `ContainerExecutor` (the `Executor` implementation), `reap_leftovers`.
- `_sandbox_probe.py` runs as the container's command: it measures the controls from inside, records them, refuses
  with exit 97 when one is absent and otherwise `exec`s the authorized command. `_prepare_env.py` builds the
  virtual environment inside the preparation container from requirements files or `pyproject.toml` (dependencies
  plus dev/test groups), never building the project.
- The `Executor` contract gains two no-op hooks, `workspace_dir()` and `release(workspace)`, called by the three
  consumers where they already create and destroy workspaces; `TestRun` gains an additive `environment` record;
  the runner adapters accept a `mount_point` so the command sees the evidence directory where it is mounted while
  the host reads the report from the workspace. Schemas bump additively: `remediation_run/1.3`,
  `architecture_report/1.1`, `evolution_report/1.2`.
- `fastapi_audit/remediate/container_cli.py`: the same `--container-*` options on the three tools;
  `--container-reap` on the remediation tool removes what an interruption left behind.

### Effective restrictions, verified twice per run

Non-root user (`65534:65534`; `host` ids allowed, root refused), read-only root filesystem, every capability
dropped, `no-new-privileges`, the runtime's seccomp profile (`unconfined` refused), `network: none` for the run,
memory without swap, CPU quota, pids limit, `nofile`/`fsize`/`cpu` ulimits, wall-clock timeout that kills the
container, `/tmp` as a size-limited `rw,noexec,nosuid,nodev` tmpfs, exactly three mounts (workspace copy rw,
evidence rw, prepared environment ro; the Docker socket, the system directories, the home directory and its
credential directories are refused as sources), an environment holding only the keys the executor sets. The
created container's configuration as the daemon reports it is compared with the specification before it starts;
the probe verifies uid, read-only root, bounding capabilities, `NoNewPrivs`, seccomp mode, interfaces and a
connection attempt, cgroup limits, the socket, `HOME` and the environment from inside before the command runs.

### Dependency preparation

Inside a container, never on the host: a volume handed to the sandbox user by the image's `chown` (root with
`CAP_CHOWN` only, nothing from the project mounted, no network), then a non-root container with the workspace
read-only, the volume read-write, the network the policy allows (`bridge` by default, `none` on demand) installing
the project's requirements files or its declared dependencies and dev/test groups; the project's build backend
never runs; `pytest` is added when the command names it and the project does not declare it; the pip cache
volume is shared only for `fixture`/`trusted` (an untrusted project could poison it); `pip freeze` of the result
is recorded. The run container mounts the environment read-only.

### Evidence

`environment` on every run: executor, isolation level, trust profile, nonce, runtime identity (versions,
operating system, kernel, cgroup version, security options, rootless, trust boundary), image identity (reference,
id, repository digests, pinned or not, provenance note), the sandbox policy, the phases with their specifications,
the daemon's view, exit codes, OOM flags, termination reasons and timings, the in-container record, the
guarantees verified for this run, the limitations, the cleanup and any rejection. The outcome still comes from the
nonce-bound runner report, never from the exit code.

### Not provided, said on every run

Protection against vulnerabilities of the container runtime or the kernel (shared); the daemon's trust boundary (a
rootful daemon is root-equivalent on its host; on the development machine the daemon runs rootful inside a Colima
VM); egress filtering during preparation; image provenance beyond the repository digest (signatures are not
verified; pin a digest); user namespace remapping; a `noexec` workspace mount (Docker bind mounts cannot be
`noexec`).

### Deliberate differences from v2.0.0

- `container` is registered next to `local`; `select_executor(..., name="container")` grants `untrusted`
  (v2.0 answered `executor_unavailable`). The local executor's refusal names `--executor container`.
- The design guarantees reword two claims to name the executor they depend on (`not_claimed`, `execution`); the
  guarantees fixtures changed accordingly.
- Every `TestRun` dictionary carries `environment` (the local executor records its host interpreter and platform).
- Nothing else: rule ids, fingerprints, suppressions, SARIF, manifests, contracts, adoption, the CLI flags and exit
  codes, and the evidence model are unchanged.

### Verified on this machine

Environment: macOS (Darwin 27.0.0) host; Docker client 29.9.2, daemon 29.5.2 **rootful inside a Colima VM** (Ubuntu
24.04.4 LTS, kernel 6.8.0-117, aarch64, cgroup v2, security options `apparmor`, `seccomp profile=builtin`,
`cgroupns`); image `python:3.13-slim` pinned at `sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb`
(Python 3.13.18, linux/arm64); CPython 3.13.5 and 3.9.6 on the host.

| Check | Result |
|---|---|
| Unit tests against the fake runtime (`tests/test_container_v21.py`, no daemon) | 19 OK: policy refusals, `docker create` arguments, `verify_effective` on 21 weakened documents, forbidden mount sources, selection without fallback, adapter path translation, additive schemas, CLI configuration, a passing run with its environment, preparation phases and cache rules, refusals before start, a tampered daemon view, the in-container probe refusing, infrastructure failures, timeout/OOM/failure/no-report, leftover reaping, the probe refusing on the host, the preparation script resolving dependencies without building |
| Opt-in real Docker (`FA_CONTAINER=1`, 8 tests in one pass) | 8 OK in 77.0 s, 32 MB host RSS: controls from inside (uid 65534, root read-only, `CapBnd` 0, `NoNewPrivs` 1, seccomp mode 2, only `lo` and `ENETUNREACH`, no socket, no credentials, no secret-looking keys, `/tmp` noexec, `memory.max` 536870912, `pids.max` 256; daemon view consistent; 14 guarantees recorded; no leftovers); a 15 s timeout kills a 120 s sleep and reports `timeout`; a 128 MiB limit ends a 1 GiB allocation without a pass; a pids limit of 32 bounds process creation; three refusals create nothing (image outside the allow-list, `network_run: bridge`, a pinned digest that does not exist); an unreachable daemon is `infrastructure_error`; labelled leftovers are reaped |
| Consumers through the container (same pass) | design of `inventory-service` as `untrusted`: dependencies prepared in a container (30 packages, no shared cache, network `bridge`), 10 tests passed in a `network: none` container, `architecture_report/1.1`; evolution `inventory-add-suppliers` as `fixture`: `verified`, shared pip cache, `evolution_report/1.2`; remediation of `remediation19` as `untrusted` with linked reproducers: 3 cases `verified` (each reproducer failed before and passed after, in containers), composition `verified`, `remediation_run/1.3`, project untouched; 32.2 s for the three |
| Preparation cost (design flow, `inventory-service`, measured twice) | cold, untrusted, no cache: prepare 21.4 s, run 3.2 s, 26.1 s wall; warm, fixture, shared cache: prepare 17.7 s, run 2.7 s, 21.6 s wall |
| Full suite, Python 3.13.5 | 603 tests OK, 19 skipped (11 as before plus the 8 Docker tests when `FA_CONTAINER` is unset); 108.0 s, 139 MB (increment 0: 576, 107.5 s, 136.5 MB) |
| Full suite, Python 3.9.6 | 603 tests OK, 33 skipped; 121.3 s, 183 MB |
| Compatibility | 59 rules, fingerprints, suppressions, JSON and SARIF, manifests 1.0 to 1.2, evolution 1.0/1.1, adoption 1.0, journal 1.0, the frozen v2.0.0 artefacts (`tests/test_compat_artifacts.py`) and the CLI flags unchanged; the only test expectations that changed are the two that asserted the container executor's absence and the run schema |
| `scripts/validate_structure.py`, `claude plugin validate`, Ruff (committed configuration), `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff, Python 3.9 byte-compilation of the new modules | pass |
| Not verified here | Podman (interface only); rootless Docker; Linux hosts with native bind-mount permissions (the mount probe and the `--container-user host` option exist for them); database-backed suites inside the container (sidecar deferred) |

## Increment 2: Expand/Contract & Safe Backfills

A PostgreSQL project generated or evolved by the tool can now receive a column that existing rows cannot satisfy,
or an index on a table with live rows, without a maintenance window and without a single big-bang revision. The
change is declared in phases (contract `fastapi-architect/evolution/1.2`), the project ships the program that fills
the rows, the state of a concrete database is measured and written down, and the NOT NULL transition is a separate,
human-authorised evolution. The fifth example, `parcels`, exists for this; `docs/guides/phased-migrations-and-backfills.md`
walks it through, `docs/architecture/adr-2.1-01-phased-migrations.md` records the decisions, and
`references/evolution-contracts.md` section 9 is the contract.

### Design

- **Three states, never confused.** *Declared*: the contract and the manifest (`generation/1.3`:
  `phased_changes[].state_declared`, `concurrent_indexes`, a `phase` per revision). *Applied*: a concrete database
  (`alembic_version`, nullability, the progress table). *Verified*: the migration evidence
  (`fastapi-architect/migration-evidence/1.0`) written by `python -m <pkg>.ops.backfill verify` for that database at
  a time. The plugin never connects to a database; the plan reads evidence, and the contract revision re-checks the
  live database when it runs.
- **Expand** (transactional revision): `ADD COLUMN ... nullable` without a server default (no rewrite), the progress
  table `<pkg>_backfill_progress` once; the model nullable, the create schema requiring the value, the read schema
  optional. **Index** (non-transactional revision): `CREATE INDEX CONCURRENTLY`, the definition compared with
  `pg_get_indexdef` (a different one fails), an invalid leftover dropped and rebuilt, a final `indisvalid` check.
  **Contract** (non-transactional revision, after authorisation): re-check of pending rows and progress state on
  the live database, `CHECK ... NOT VALID`, `VALIDATE CONSTRAINT`, `SET NOT NULL`, `DROP CONSTRAINT`, each step
  idempotent; downgrade `DROP NOT NULL`. Online, not lock-free: the lock profile of every phase is printed in the plan.
- **Declarative backfills only**: `constant`, `copy_column`, `uuid`, `now`. Identifiers come from names the contract
  validated; values are typed literals bound as parameters; no SQL travels from the contract.
- **The generated runner** (`src/<pkg>/ops/backfill.py`): keyset batches on the primary key with `FOR UPDATE SKIP
  LOCKED`, `UPDATE ... WHERE column IS NULL`, rows and checkpoint in one transaction; `--max-batches`,
  `--max-seconds`, `--batch-pause-ms`; one worker per operation (advisory lock plus worker record); a checksum of the
  definition guards the checkpoint; a table with row-level security is refused unless the role bypasses it;
  `complete` only after counting zero pending rows (up to three passes); a run on a complete operation re-counts and
  reopens it for exactly the rows that appeared; exit codes 0–7 with a meaning each. Credentials come from
  `<PKG>_MIGRATIONS_DATABASE_URL` or `<PKG>_DATABASE_URL`.
- **Contract is a human decision**: `--authorize-contract` on the command line and `authorization.contract.declared_by`
  in the contract, evidence no older than `max_evidence_age_hours` (24), and twelve named preconditions in the plan
  (`contract_authorized` … `contract_indexes_valid`, `contract_reversible`); never the continuation of a backfill;
  no contract phase drops anything.

### Contracts and schemas (all additive)

| Artefact | Change |
|---|---|
| `fastapi-architect/evolution/1.2` | `add_fields[].backfill`, `add_indexes`, `contract_fields`, `authorization.contract`; refused with the path under 1.0 and 1.1, which keep their meaning |
| `fastapi-architect/generation/1.3` | `phased_changes`, `concurrent_indexes`, `migrations[].phase / transactional / reversible`; 1.0 to 1.2 still read |
| `fastapi-architect/migration-evidence/1.0` | new: written by the generated runner, read by the plan |
| `evolution_report/1.3` | `contract_authorization`; `plan.migration` gains `phases`, `backfills`, `contract`, `head_after`, `revisions`; `plan.phased` |
| CLI | `evolve_fastapi.py plan|apply --authorize-contract [--evidence FILE]` |

### Deliberate differences from v2.0.0

- Every new manifest is `generation/1.3`; the four committed snapshots were regenerated and only their manifests
  changed (the per-file oracle against the v2.0.0 engine shows every source file byte-identical).
- A required field with a declared `backfill` is `compatible` and planned as a phased change; without it the plan is
  blocked as before.
- `tests/test_migrations.py` of a phased project treats the progress table as one of its own; `tests/test_backfill.py`
  and the `ops` package exist only in projects with phased changes.
- The two test expectations that asserted `generation/1.2` for fresh manifests now assert 1.3; the two that asserted
  `evolution_report/1.2` assert 1.3.

### Verified on this machine

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 and 3.9.6, PostgreSQL 16.10 (Homebrew) as throwaway clusters,
uv 0.11.28, Ruff 0.17.3. Full detail and every measured number: `plugins/fastapi-architect/examples/generated/VERIFICATION.md`
(section "v2.1 increment 2").

| Check | Result |
|---|---|
| Unit module `tests/test_migrations_v21.py` (no database) | 18 OK on 3.13 (15 s) and on 3.9 (21 s): contract validation and refusals (SQL, wrong types, unknown strategies, phased keys under older schemas), planning of expand/index/contract, the generated runner and tests Ruff-clean, no SQL from contracts, in-memory refusals, manifest states, contract preconditions on evidence documents, the example files |
| Real PostgreSQL, `FA_DESIGN_PG=1 tests/test_migrations_v21_postgresql.py` | 4 OK in 42.2 s, 127 MB peak RSS. The slice: 2 500 live rows; expand evolution verified (18 generated tests) and applied in 7.6 s; expand + concurrent index on the production database in 0.38 s; a clean stop after 3 batches of 200; two workers (`[0, 3]`, exactly one progressed to 800); a SIGKILL with the checkpoint equal to the rows filled; resumption to `complete` with 2 500 rows exactly once (0.32 s); concurrent writes: 3 application values preserved, 40 rows inserted by an old version during the run, 2 508 rows in 18 batches / 2 passes / 0.27 s, 29 late rows reopened and filled by the next run; the other column reopened through the CLI and filled (2 540 rows, 20 batches); corrupt and foreign checkpoints refused (exit 4), unknown operation (7); evidence `ok`; contract blocked without authorisation and with the stale sample; contract evolution verified (14 passed, 4 skipped) and applied in 7.6 s; the revision refuses a reappeared NULL (`still NULL`), then applies in 0.35 s (both columns `NOT NULL`, no transitional constraint left, one head); downgrade keeps every row; a fresh database applies the four revisions in 0.35 s with evidence `ok`. Index with a different definition refused, invalid index rebuilt (0.34 s). Connection failure exit 2; time limit never success. RLS table refused as the application role (exit 5), filled as the owner |
| Opt-in PostgreSQL modules of v2.0 under the same cluster support (`tests/test_design_v20_postgresql.py`, `tests/test_acceptance_v20.py` with `FA_DESIGN_PG=1`) | 9 OK in 66.0 s, 132 MB; 3 OK in 36.4 s: the generated PostgreSQL backends and the v2.0 acceptance scenarios are unchanged by the phased machinery |
| Full suite, Python 3.13.5 | 625 tests OK, 23 skipped (the 19 of increment 1 plus the 4 PostgreSQL phased tests when `FA_DESIGN_PG` is unset); 86.3 s, 133 MB (increment 1: 603 tests, 108.0 s, 139 MB) |
| Full suite, Python 3.9.6 | 625 tests OK, 37 skipped; 91.2 s, 185 MB (increment 1: 603, 121.3 s, 183 MB) |
| Defects the real cluster found before the slice passed | six in the generated runner or its generated tests (autobegin before the advisory lock; driver exceptions escaping with exit 1; `test_verify` running one operation; a complete operation ignoring late NULL rows; insert-based tests after the contract; the contract revision's empty-table record refused): all fixed in the template and listed in `VERIFICATION.md` |
| Compatibility | 59 rules, fingerprints, SARIF, suppressions, the frozen v2.0.0 artefacts (`tests/test_compat_artifacts.py`), evolution contracts 1.0/1.1 and their examples, adoption, remediation, manifests 1.0 to 1.2, the journal, the local and container executors and the CLI flags unchanged; the auditor's code is untouched by this increment (no precision re-run needed; the holdout corpus was not re-scored) |
| `scripts/validate_structure.py`, `claude plugin validate`, Ruff (committed configuration), `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff and the new files, Python 3.9 byte-compilation of the new modules and tests | pass |
| Not verified here | tables beyond a few thousand rows; backfills as the application role under RLS (refused by design); two real application versions (a thread stood in for the old one); the database inside the container executor (sidecar deferred); Podman |
