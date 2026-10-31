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
