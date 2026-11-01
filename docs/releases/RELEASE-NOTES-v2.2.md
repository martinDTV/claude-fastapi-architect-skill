# FastAPI Architect v2.2.0 — Safe Evolution II (in progress)

Status: in progress on top of the published v2.1.0 (`7aff2b1`, release at `219383d`). The scope was decided on
2026-10-31 after the discovery recorded in [`docs/architecture/discovery-v2.2.md`](../architecture/discovery-v2.2.md)
(section 11): seven increments, each authorised separately. This document grows one section per increment. The
version constant, the plugin manifests and the generated manifests stay at 2.1.0 until the release increment bumps
them.

## Increment 0: Foundations for Safe Evolution II

Nothing a user runs changes its outcome in this increment: the five committed snapshots regenerate byte-identically,
the per-file oracle against the v2.1.0 engine shows no difference on any specification, the evolution and
remediation behaviour is unchanged. What the increment adds is the ground the next increments stand on, each piece
pure, tested on an adversarial corpus and not yet wired into a user-facing operation except where noted.

### Span-exact edits (`fastapi_design/spans.py`)

Three primitives that edit an existing Python file without ever re-rendering it, next to the append-only ones of
`transforms.py`:

| Primitive | What it does | Proof it carries |
|---|---|---|
| `insert_statement` | one statement inside a function body (module level too), after the last call to a named callee or at the end of the body, at the anchor's indentation | the body grew by exactly one statement and every other node is unchanged (`ast.dump`), every byte outside the inserted lines is identical, the comment tokens are the same |
| `insert_keyword_argument` | `name=value` on one existing statement-level call, located by callee, enclosing function, index or first argument; follows the style it finds (inline, or one argument per line with a trailing comma) | the call gained exactly that keyword and nothing else changed; bytes outside the call's span identical; comments identical |
| `insert_parameter` | one parameter on one signature, where Python's ordering allows it (end of the positional list, end of the keyword-only section, before `**kwargs`) | the signature gained exactly that parameter, body and decorators unchanged, the rest of the module unchanged; bytes outside the signature identical |

Refusals, named: an ambiguous or missing target, a call or signature whose span contains a comment (refused rather
than re-rendered), a keyword already present with another value (never overwritten), `**kwargs` unpacking in the
call, a parameter without a default after parameters with defaults, a duplicate parameter name, a protected region.
No-ops with evidence: the same keyword with the same value, an identical parameter, an identical statement already
in the body. CRLF files stay CRLF; tab-indented bodies get tabs. `ProtectedRegion` is the explicit representation of
lines an operator froze; `detect_formatter` and `format_check_command` prepare the formatter-stability check the
verification copy will run in a later increment (nothing is executed here).

### The pure three-way merge (`fastapi_design/merge.py`)

`merge3(base, ours, theirs)` on lines (`difflib`): disjoint hunks both applied, two insertions at one point a
conflict unless identical, overlapping edits a conflict, an insertion at the edge of a replaced block ordered rather
than conflicting, identical changes applied once, CRLF kept; **no text is produced when any conflict or protected
violation exists**. Protected regions refuse hunks of the *generated* side only. `merge_python` adds the structural
proof over the top-level statements, matched by identity (a definition by its name, an assignment by its targets,
anything else by its text): added statements present, a statement changed by one side holds that side's version, one
changed by both is at least not the base version (reported under `both_modified`), untouched ones unchanged; a merge
that fails it is a conflict even when textually clean, and a statement both sides added that appears twice is
reported as `duplicate_additions`. `merge_json` merges `.audit-fastapi.json`-like documents by key (added on one
side: taken; changed on one side: taken; both to the same value: once; both differently, or removed versus changed: a
conflict named by its JSON path). Not wired into any operation yet: the planner of increment 3 will call it under
`files.allow_merge`.

### The project lock and journal 1.1 (`fastapi_design/locking.py`, `journal.py`)

One writer at a time per project: `.fastapi-architect/ops/LOCK`, created with `O_EXCL`, holds pid, host, start time,
operation kind and description and a token. **Fail fast by default**: `apply`, `adopt` and `recover` raise
`LockHeld` naming the holder and the reason (`held`, `stale`); `apply` reports `applied.state: locked`, `recover`
exits 3. Bounded wait on request (`recover --wait SECONDS`; `wait_s` in the API). A stale lock (holder pid gone on
this host, or older than six hours from another host) is reported and **never broken silently**: `evolve_fastapi.py
ops unlock` removes it after naming it and refuses a live one. The journal is `fastapi-architect/operation/1.1`,
additive: `steps` (what produced each file: `regenerate`, `transform`, `manifest`; `merge` later, with evidence
digests) and `parent_op`; the file protocol and the operation identity are unchanged, so 1.0 journals are still read
and recovered. `evolve_fastapi.py ops list | show | unlock` print pending operations, their steps, the files'
current classification and the lock.

### Adversarial corpus and compatibility fixtures

`tests/fixtures/transforms22/app_factory.py` (decorators with keyword arguments, `Annotated` dependencies, a
`TYPE_CHECKING` block, a call with a comment inside it, multi-line calls and signatures with trailing commas,
keyword-only and `**kwargs` signatures, a nested class; CRLF and tab variants derived in the tests) and inline merge
cases (`tests/test_merge_v22.py`). `tests/fixtures/compat/` gains what v2.1.0 wrote: the `parcels` project with its
manifest 1.3, the two phased evolution contracts 1.2, a migration evidence 1.0 and an evolution report 1.3; the
compatibility tests prove each is read and still means the same.

### Defects the corpus found before the increment closed

- `acquire` created the lock directory once before its retry loop; a holder releasing at that moment removes the
  empty directory, so a waiting acquirer hit `FileNotFoundError` instead of taking the lock. The directory is now
  recreated on every attempt and the vanished-directory case retries.
- The first structural proof of `merge_python` treated a function modified by both sides as two "added" nodes and
  declared the clean merge lost; the proof now matches top-level statements by identity.
- `merge_json` mistook a key added by one side for a key removed by one side and changed by the other.

### Deliberate differences from v2.1.0

- Journals are written as `operation/1.1` (1.0 readers of this tool keep working on 1.0 journals; a v2.1 reader
  would report a 1.1 journal as needing attention, which is the safe direction).
- `apply` may end in `applied.state: locked`; `recover` and `adopt` exit 3 on a held lock; `ops` is a new subcommand.
- Nothing else: contracts, manifests, rule ids, fingerprints, suppressions, formats, the five snapshots and the
  generated code are unchanged.

### Validation

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 and 3.9.6, PostgreSQL 16.10 as throwaway clusters, Ruff 0.17.3.

| Check | Result |
|---|---|
| New modules on the adversarial corpus | `tests/test_spans_v22.py` 16, `tests/test_merge_v22.py` 19, `tests/test_journal_v22.py` 12 (lock lifecycle, fail-fast, bounded wait, stale and unreadable locks, another host's age rule, a write refused under a held lock, two processes racing, two threads serialising on disjoint files, steps surviving an interruption and a forward recovery, a 1.0 journal, identity independent of steps, the `ops` subcommands); all OK on 3.13 and 3.9 |
| Compatibility of the v2.0.0 and v2.1.0 artefacts | `tests/test_compat_artifacts.py` 13 OK: manifests 1.0, 1.1 and 1.3, evolution 1.0, 1.1 and 1.2, journal 1.0, adoption 1.0, audit report 1.7 fingerprints, migration evidence 1.0, evolution report 1.3 (same revisions and operation ids reproduced) |
| Generation equivalence | per-file oracle against the v2.1.0 engine (`7aff2b1`) on every specification of `examples/specs` and `tests/fixtures/design20/specs` and on every example evolution plan: 259 files, 0 differing; the five committed snapshots regenerate with 0 files written |
| Existing behaviour of evolution, adoption, recovery and remediation | `test_evolve_v20.py` 26, `test_evolve_v20_adoption.py` 22, `test_evolve_v20_recovery.py` 18, the remediation suites inside the full run: OK with the lock in place |
| Full suite, Python 3.13.5 | 680 tests OK, 26 skipped (the 51 new tests included); 137.4 s, 140 MB |
| Full suite, Python 3.9.6 | 680 tests OK, 41 skipped; 142.9 s, 184 MB |
| Real PostgreSQL (the modules whose `apply` writes through the journal) | `test_migrations_v21_postgresql.py` 4 OK (60.0 s), `test_acceptance_v21.py` 4 OK (67.6 s), `test_acceptance_v20.py` with `FA_DESIGN_PG=1` 3 OK (51.0 s) |
| Auditor | no file under `fastapi_audit` changed: 59 rule ids, fingerprints, suppressions, SARIF and precision identical by construction |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate` (marketplace and plugin), Ruff, `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff and every new file | pass; no cache or temporary file added |
| Not run | the real-Docker suite (the executor is untouched by this increment; its fake-runtime tests ran inside the full suite) |

## Increment 1: Safe Column Renames, Expand & Transition

A field of a PostgreSQL project can be renamed without a maintenance window and without the previous application
version knowing: the rename is a phased change whose expand evolution adds the new column next to the old one, a
**sync trigger** keeps the pair equal for every write of either version, the generated transitional code accepts and
exposes both names while reading the old column, and the v2.1 backfill runner reconciles the rows written before the
trigger. Nothing is renamed in place, retired or dropped: retire (the new-name-only version) and contract (the drop)
are later increments, each an authorised evolution. The decision record is
[`docs/architecture/adr-2.2-01-safe-renames.md`](../architecture/adr-2.2-01-safe-renames.md); the guide section is in
[`docs/guides/phased-migrations-and-backfills.md`](../guides/phased-migrations-and-backfills.md); the contract is
section 10 of `references/evolution-contracts.md`.

### Design

- **Three versions, one database.** A knows the old name only; B knows both; C (not generated in this increment) will
  know the new one. A and B coexist on the expanded schema; the old column is the source of truth until retire.
- **Why a trigger, and which.** A does not perform a dual write, so only the database can reflect its writes into the
  new column. The expand revision creates, per rename, a `BEFORE INSERT OR UPDATE FOR EACH ROW` trigger: on insert
  the non-NULL side fills the other, the old one first; on update, the column that changed fills the other, the old
  one winning when one statement sets both to different values. It only assigns to `NEW` (no second statement, no
  recursion), runs inside the writer's transaction (a rollback covers both columns), carries no privilege
  (`SECURITY INVOKER`) and leaves row-level security to the policies, which still check the final row. It is named
  after the operation, recorded in the manifest and the runner's definitions, reported by the evidence and exercised
  by the generated tests; the auditor's migration rules report nothing for it (0 findings before and after,
  measured on the fifth example).
- **Transitional code (B).** The model maps both columns (the new one nullable, the old one as before); request
  schemas accept the old name through `AliasChoices`; the read schema fills the new field from the old column and
  exposes the old name marked deprecated; services write both on create and update; the module tests cover both
  names and the generated backfill tests exercise the trigger in both directions.
- **Backfill reused.** Strategy `mirror_column` with the pending predicate `new IS DISTINCT FROM old`: the runner
  of v2.1 unchanged in its semantics (keyset batches, `FOR UPDATE SKIP LOCKED`, checkpoint with the rows, one worker,
  `complete` after a zero count, reopened for rows that differ later). The evidence (1.1) adds `mismatch_rows`,
  `columns_agree`, `source_of_truth` and the trigger state; a missing or disabled trigger is a problem.
- **Refused by name**: `in_place` (`ALTER TABLE … RENAME COLUMN` is not online across versions), any sync but the
  trigger, renames of identity, ownership, tenant and timestamp columns and of foreign keys, a target that exists, a
  column already in transition, renames on in-memory projects or new tables, and the retire or contract of a rename.
  A repeated rename is a no-op.

### Contracts and schemas (all additive)

| Artefact | Change |
|---|---|
| `fastapi-architect/evolution/1.3` | `modify_modules[].entities[].rename_fields[] {from, to, strategy: expand_contract, sync: trigger}`; 1.0–1.2 keep their meaning; `rename_fields` under them is refused with the path |
| `fastapi-architect/generation/1.4` | `phased_changes[]` entries of `kind: rename` carry `from_column`, `from_field`, `sync`, `source_of_truth`, `read_policy`, `write_policy`, `transition`; `history[].phased.renamed`; 1.0–1.3 still read |
| `fastapi-architect/migration-evidence/1.1` | `operations[].kind`, `source_column`, `source_of_truth`, `mismatch_rows`, `columns_agree`, `sync {mechanism, trigger, present, enabled}`; 1.0 still read |
| `evolution_report/1.4` | `plan.migration.renames[]` and the `transition` entry of `plan.migration.phases` |
| Example | `examples/evolutions/parcels-rename-label.json` (two renames on the fifth example) |

### Deliberate differences from v2.1.0

- Every new manifest is `generation/1.4`; the five committed snapshots were regenerated and only their manifests
  changed.
- `rename_fields` at entity level is a phased change under 1.3 instead of an "unsupported" error (under 1.0–1.2 it is
  refused as needing 1.3); `remove_fields` and `change_fields` stay unsupported.
- The expand revision of a rename contains raw DDL for the trigger; the audit of the evolved project stays at 0 findings.
- The test expectations that pinned `generation/1.3` and `evolution_report/1.3` for fresh artefacts pin 1.4.

### Validation

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 and 3.9.6, PostgreSQL 16.10 as throwaway clusters, uv 0.11.28,
Ruff 0.17.3. The measured numbers of the coexistence run are in `plugins/fastapi-architect/examples/generated/VERIFICATION.md`
(section "v2.2 increment 1").

| Check | Result |
|---|---|
| Unit module `tests/test_renames_v22.py` (no database) | 14 OK on 3.13 and 3.9: schema 1.3 gating, refusals by name, the plan as a phased mirror operation with its sync names and policies, the transitional model, schemas, services, the expand revision (trigger DDL, no `RENAME COLUMN`, no drop), the runner definitions and generated tests, the manifest 1.4 record, the no-op on repetition, the refusal of retire and contract, the evolved project Ruff-clean |
| Real PostgreSQL, `FA_DESIGN_PG=1 tests/test_renames_v22_postgresql.py` | 4 OK in 43.0 s, 124 MB. A (the committed snapshot) and B (the evolved project) as two processes from their own environments on one expanded database with 1 500 live rows: A's inserts and updates mirrored by the trigger; B reads A's rows before the backfill and writes both columns, accepting the former name; A reads B's rows; 30 interleaved updates of one row from both processes leave the pair equal; a rolled-back write touches nothing; NULLs agree; the backfill killed after 300 rows and resumed while A inserted 20 rows: `complete` with 1 500 rows in 12 batches, 0 mismatches; the second rename's backfill (1 000 rows, the 500 NULL weights already agreeing); evidence 1.1 `ok` with `columns_agree` and the triggers enabled; both versions working after the backfill. Locked rows skipped and revisited; drift behind a disabled trigger named by `verify` and fixed by a run that reopens the operation; a disabled trigger reported; corrupt and foreign checkpoints refused; the expand revision downgraded without data loss. A hand-made column of the new name failing the revision with the database unchanged; stale baseline, manual change on a regenerated integration point, altered historical revision, two heads and a premature contract blocked. A rename on a row-level-security table mirroring the application role's writes under the policy; the tenant column refused |
| The generated suite of the evolved project, in the verification copy | 21 tests passed (the former-name create and update tests, the sync-trigger test in both directions, the backfill tests with the mirror predicate); audit 0 → 0 findings |
| Generation equivalence | per-file oracle against the published engine on every specification: the seven fresh projects byte-identical (272 files compared); in the evolution plans only the manifests (1.4) and, for the phased example, the regenerated runner, its definitions and its tests (evidence 1.1, mirror strategy) differ; the five snapshots regenerate with their manifests only |
| Existing behaviour | evolution (66), design (105), compatibility (13), container (27), migrations (18) suites OK; PostgreSQL regression: `test_migrations_v21_postgresql.py` 4 OK (57.8 s), `test_acceptance_v21.py` 4 OK (65.2 s), `test_acceptance_v20.py` 3 OK (51.2 s), `test_design_v20_postgresql.py` 9 OK (88.4 s) |
| Full suite, Python 3.13.5 | 698 tests OK, 30 skipped (the 18 new tests included; the 4 PostgreSQL rename tests skip without `FA_DESIGN_PG`); 137.9 s, 135 MB |
| Full suite, Python 3.9.6 | 698 tests OK, 45 skipped; 145.2 s, 186 MB |
| Auditor | no file under `fastapi_audit` changed: 59 rule ids, fingerprints, suppressions, SARIF and precision identical by construction |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate`, Ruff, `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff and every new file | pass |
| Not run | the real-Docker suite (the executor and its integration are untouched; its fake-runtime tests ran inside the full suite) |
| Defects the scenarios found before the increment closed | the trigger state read as the single-byte `"char"` type (asyncpg returns bytes: every trigger looked disabled); the generated trigger test inserting only the renamed column (a NOT NULL neighbour failed the insert); generated lines over the 120-column limit; all fixed and covered |
