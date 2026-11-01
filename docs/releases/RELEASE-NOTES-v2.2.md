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

## Increment 2: Retire & Controlled Contract

The two evolutions that finish a rename. **Retire** moves the source of truth to the new column: the generated code
(state C) knows the new name only and the retire revision carries the old column's NOT NULL and default to the new one
online, swaps the sync function under the same trigger and stamps `truth=<new> retired_at=<now>` in its comment;
nothing is dropped and B keeps working. The **destructive contract** drops the old column, its sync trigger and
function, `RESTRICT`, in one transaction, and only after sixteen named preconditions evaluated by the plan from fresh
evidence and re-checked by the revision on the live catalog under an exclusive lock. It is the only destructive
operation of the tool: disabled by default, exceptional, explicit (`--authorize-contract` plus a distinct
`--confirm-irreversible "drop <table>.<column>"`, an approver's identity and an explicit declaration that older
versions stopped), verifiable (every condition has a name in the plan) and honest about what its downgrade can do.
The decision record is [`docs/architecture/adr-2.2-01-safe-renames.md`](../architecture/adr-2.2-01-safe-renames.md)
(decisions 7 to 10), the guide section is "Retire and the destructive contract" in
[`docs/guides/phased-migrations-and-backfills.md`](../guides/phased-migrations-and-backfills.md), the contract is section 11
of `references/evolution-contracts.md`.

### Design

- **Retire (B → C), reversible, no flag.** `retire_fields[] {name, operation}` under contract 1.4 needs
  `authorization.contract.declared_by` and B's evidence: trigger present and enabled, its truth marker on the old
  column, zero rows where the columns differ, backfill complete, the transitional version declared, evidence fresh and
  at the manifest head. It regenerates the code under the new name (no alias, no dual write, no deprecated field; the
  generated schema-drift test tolerates the retired old columns) and appends a `retire` revision (autocommit: the
  re-checks, `CREATE OR REPLACE FUNCTION`, `_set_not_null_online`, `SET DEFAULT`, `COMMENT ON FUNCTION`); its
  downgrade is `DROP NOT NULL`, `DROP DEFAULT` and the comment back. The manifest entry becomes `state_declared:
  retire` with `retire_revision`, the policies on `to_column` and `compatible_tags`.
- **Contradictions are refused, not resolved.** The sync trigger of increment 1 let the old column win when one
  statement set both names to different values; it now raises (SQLSTATE 23514, the writer's transaction rolls back),
  with the single exception of an INSERT where a column default made both non-NULL, where the declared truth wins.
  The transitional application refuses, at its own boundary, a request that sends both names of a field, equal or
  not (422, `model_validator(mode="before")`: one name per request). Generated tests cover both.
- **B and C coexist; A is tolerated, then identified.** C writes the new column and the trigger fills the old one; B
  writes both and reads the old one, kept equal; A's writes to the old column are still mirrored (nothing is lost) but
  A is outside the retire contract. Every generated application now announces `application_name = <package>@<12
  hex>` (a hash of its declared phased states, `core/db.py`): A, generated before v2.2, announces nothing; B and C
  carry different tags; `verify` lists the sessions of the database grouped by name.
- **The destructive contract (C → D).** `contract_fields` on a retired rename plus `authorization.destructive
  {approved_by, old_versions_stopped, min_retire_age_hours (default 24, 0 allowed), ignore_sessions,
  reviewed_functions}`; the invariant `no_destructive_schema_change` must be left out on purpose. Sixteen named
  preconditions (`references/evolution-contracts.md`, section 11): identity; retire declared and applied (the truth
  marker in the catalog, the evidence written by C's runner); evidence fresh and newer than `retired_at`; equal
  columns; backfill complete; the retired version declared; every connected session identified (a `compatible_tags`
  tag, the runner's own name or an `ignore_sessions` name) plus the operator's declaration; the minimum age; NOT
  NULL, type and default carried; no dependent in `pg_depend` beyond the column's own default and NOT NULL and no
  trigger or user function naming the column unless reviewed by name; one Alembic head on disk and in the database;
  no managed file edited by hand; `--authorize-contract` with `declared_by`; the approver; `--confirm-irreversible`
  with the exact phrase the plan prints; no lock held by another process. The `drop` revision is one transaction:
  `SET LOCAL lock_timeout = '5s'`, `LOCK TABLE … ACCESS EXCLUSIVE`, every database-side check again (including the
  catalog dependents through `pg_depend` and the sources of the table's other triggers), `DROP TRIGGER`, `DROP
  FUNCTION`, `DROP COLUMN … RESTRICT`. `CASCADE` is never generated. The approver and the phrase are recorded in the
  revision's docstring and in the manifest.
- **Reversibility, kept apart and said plainly.** DDL: conditional (the downgrade recreates the column, copies the new
  column's current values back, restores NOT NULL and default and recreates the trigger; offline). Data: the dropped
  content is not recoverable from the database (take a backup before the upgrade). Compatibility: stop, downgrade,
  redeploy B; no online rollback. The plan's destructive phase carries `ddl_reversibility`, `data_recovery` and
  `compatibility_restoration` as three separate statements.
- **Refused by name** (new): `rename_indexed_column_unsupported` (a concurrent index on the source column would
  vanish with it), `contract_rename_retire_first`, `retire_rename_only`, `retire_operation_known/pending`,
  `no_destructive_schema_change`, and every `retire_*`/`contract_*` precondition above. A second contract of a
  contracted operation is `contract_operation_pending`.

### Contracts and schemas (all additive)

| Artefact | Change |
|---|---|
| `fastapi-architect/evolution/1.4` | `retire_fields[] {name, operation}`; `contract_fields` on a retired rename; `authorization.destructive {approved_by, old_versions_stopped, min_retire_age_hours, ignore_sessions, reviewed_functions}`; `rename_fields` accepted under 1.3 and 1.4; 1.0–1.3 keep their meaning |
| `fastapi-architect/generation/1.5` | `application_tag`; rename entries in state `retire` (`retire_revision`, `compatible_tags`, policies on `to_column`) and `contract` (`dropped`, `destructive`); `history[].phased.retired/dropped`; the `retire` phase and `destructive`/`reversible: "conditional"` on migration entries; 1.0–1.4 still read |
| `fastapi-architect/migration-evidence/1.2` | rename operations: `source_present`, `columns`, `sync.function/truth/retired_at`, `dependents`, `unmanaged_dependents`, `other_triggers`, `functions_mentioning`; document: `sessions`, `tool_session`; 1.0 and 1.1 still read (a drop needs 1.2) |
| `evolution_report/1.5` | `plan.migration.retires[]`, destructive `contract[]` entries, the `retire` and destructive `contract` phases, `plan.phased.retiring/dropping`, `contract_authorization.confirm_irreversible/approved_by`, classification `destructive` |
| CLI | `evolve_fastapi.py plan|apply --confirm-irreversible PHRASE` |
| Examples | `parcels-retire-label.json`, `parcels-contract-label.json`, `parcels-rename-evidence.sample.json`, `parcels-retire-evidence.sample.json` |

### Deliberate differences from v2.1.0 and from increment 1

- Every generated `core/db.py` now announces an `application_name` (one line and a `connect_args`); the five committed
  snapshots were regenerated: the three PostgreSQL projects changed `core/db.py` and every manifest moved to 1.5.
  `src/<pkg>/core/db.py` joined the default integration points an evolution may regenerate (its tag changes with the
  phased states); a contract with an explicit `files.allow_modify` list must include it for such evolutions.
- The sync trigger refuses contradictory statements instead of letting the old column win (increment 1 never shipped).
- The generated contract revision of v2.1 (NOT NULL) wraps one line that exceeded the project's 120 columns; the
  phased example's plan differs from the published engine in that file, the runner, its definitions, its tests and
  `core/db.py`, all explained; the seven fresh projects are byte-identical to the published engine except `core/db.py`.
- Test expectations pin `generation/1.5`, `evolution_report/1.5` and `migration-evidence/1.2` for fresh artefacts; the
  increment-1 test that expected retire and contract to be refused now expects `contract_rename_retire_first`.

### Validation

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 and 3.9.6, PostgreSQL 16.10 as throwaway clusters, uv, Ruff 0.17.3.
The scenario list and the measured numbers of the PostgreSQL run are in
`plugins/fastapi-architect/examples/generated/VERIFICATION.md` (section "v2.2 increment 2").

| Check | Result |
|---|---|
| Unit module `tests/test_retire_contract_v22.py` (no database) | 12 OK on 3.13 (20.6 s) and 3.9 (27.4 s): contract 1.4 and the destructive block by name, the examples and the evidence samples, evidence 1.0–1.2 still read, every retire precondition broken one at a time, the sixteen drop preconditions all evaluated and each adversarial condition blocking by its name (B's tag connected, an unidentified session, a console, differing rows, stale evidence, retire not applied on the database, too young, trigger disabled, a view, an index, a foreign key, an unknown trigger, a user function, another head, two heads, NOT NULL or type not carried, evidence written by B's runner, the wrong version, evidence 1.1, another operation), the reviewed function and the ignored session accepted by name, a zero wait, the lock held by another process, a hand-edited file, the drop revision's text (one transaction, RESTRICT, never CASCADE, lock timeout, the approver and the phrase in the docstring, the conditional downgrade), C's code without the old name, the application tags of A, B and C, the manifest and history records, a second contract refused, and both generated projects passing their own Ruff configuration and compiling |
| Real PostgreSQL, `FA_DESIGN_PG=1 tests/test_retire_contract_v22_postgresql.py` | 3 OK in 94.5 s, 134 MB (final run, with the unit suite running alongside). A as v2.1.0 published it, B and C as three processes from their own environments on one database: B and C coexist on the retire schema (C writes the new name only and the trigger fills the old column; B writes both; partial updates and NULLs consistent; 30 interleaved updates leave the pair equal in 0.06 s); B refuses both names in one request (422) and the trigger refuses one statement setting both to different values (23514) with the row untouched; A tolerated and listed as an unidentified session; the retire revision applied in 2.6 s, downgraded and upgraded again; the destructive contract refused while A or B are connected, blocked without the phrase (the command line prints it), verified in a copy (18.6 s) and refused on the live database by a view, an index, an unknown trigger naming the column, a disabled trigger, differing rows and a lock holder (`lock timeout` after 5.7 s), each leaving `alembic_version`, the columns and the triggers intact; the drop in 0.56 s while C writes; C keeps working, B answers 500; the downgrade (0.54 s) recreates the columns with 0 differing rows and B works again; the wrong order refused on a live project |
| The generated suites of the evolved projects, in the verification copies | C: 20 selected, 16 passed, 4 skipped, 0 failed (the trigger mirror and contradiction tests, the schema-drift test tolerating the retired columns); D (contracted): 20 selected, 14 passed, 6 skipped, 0 failed, its migrations run through the drop on a fresh database; B's own suite 23 passed (the one-name-per-request test among them); audit: no new blocking finding in any of them |
| Generation equivalence | per-file oracle against the published v2.1.0 engine on every specification: the five fresh projects identical except `core/db.py` in the three PostgreSQL ones (179 of 182 files); in the evolution plans only the manifests (1.5) differ, except the phased example (`core/db.py`, the runner, its definitions, its tests: evidence 1.2 and the tags) and the contracts the published engine cannot read (1.3, 1.4) |
| Existing behaviour | full suite 713 OK on 3.13 (177.3 s, 140 MB) and 713 OK on 3.9 (191.9 s, 184 MB); PostgreSQL regression: `test_migrations_v21_postgresql.py` 4 OK (60.1 s), `test_acceptance_v21.py` 4 OK (75.9 s), `test_acceptance_v20.py` 3 OK (57.3 s), `test_design_v20_postgresql.py` 9 OK (93.5 s), `test_renames_v22_postgresql.py` (increment 1) 4 OK (47.2 s) after its premature-contract expectation moved to `contract_rename_retire_first` |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate` (marketplace and plugin), Ruff, `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff and every new file | pass; the Docker executor was not modified, so the container tests kept their place in the suite and no real container run was repeated |

Limits, stated: the sessions check is a snapshot of `pg_stat_activity` (instances that connect intermittently are
invisible; a connection pooler in transaction mode may hide the application name); the minimum age is a policy, never
proof; the function scan is textual (dynamic SQL and functions that name neither the table nor the column are not
seen); the downgrade's full-table copy is offline and was not timed at scale; a connection failure inside the drop
transaction was not injected (PostgreSQL rolls the transaction back); nothing here demonstrates a distributed
deployment. The version stays 2.1.0 until the release increment; increments 3 to 6 need their own authorisation.

### Acceptance hardening of the destructive contract (2026-11-01)

A strictly bounded second pass over the only destructive operation, before increment 3: negative tests on a real
PostgreSQL (`tests/test_destructive_acceptance_v22_postgresql.py`, opt-in) and fixes for the defects they
demonstrated. No capability was added. Before and after each defect is recorded in
`plugins/fastapi-architect/examples/generated/VERIFICATION.md` (section "acceptance hardening").

| Defect demonstrated before the fix | Fix |
|---|---|
| A field with a declared default, renamed: `INSERT … (code, status) VALUES ('T-1', 'closed')` through the new name ended as `state = 'open', status = 'open'`; the sync trigger took the old column's default for a value and the explicit write was lost silently (the "truth wins on INSERT with a default" branch of increment 2) | renames of fields that declare a default are refused by the planner (`rename_defaulted_column_unsupported`); the trigger has no precedence branch left: a statement that sets both names to different values is refused in every state |
| A generation that maps the old column (B) reconnecting between the plan and `alembic upgrade head`: the drop revision dropped the column with B connected (the sessions were checked by the plan only) | the drop revision re-checks the connected sessions on the database clock before locking (`COMPATIBLE_TAGS`, `IGNORED_SESSIONS` and the runner's own name generated into the file) and refuses, naming the sessions |
| Evidence that aged past the contract's `max_evidence_age_hours` between the plan and the upgrade: the revision dropped the column | the revision re-checks the age of the evidence the plan read (`EVIDENCE_GENERATED_AT`) and the age of the retire (`retired_at` against `min_retire_age_hours`) and refuses |
| The downgrade of the drop (recreate, copy back, trigger) set no `lock_timeout`: queued behind traffic indefinitely (by inspection of the generated revision) | the downgrade sets the same `lock_timeout` and fails fast; any failure inside it rolls the whole downgrade back (verified with a trigger-name collision: the recreated column is gone again and `alembic_version` did not move) |

Also recorded: the plan now states what is known about deployments in five separate parts
(`plan.migration.phases[contract].assurance`: human declaration, observed sessions, schema state, automated
verification, residual uncertainty) and the evidence document carries `limits[]`; neither presents a wait, a
declaration or a snapshot as proof that older instances are gone.

Verified on the throwaway cluster (numbers in the verification record): every write pattern of the transition
(old name only, new name only, both equal, both different, partial updates, explicit NULLs on the nullable and on
the NOT NULL pair, rollback, two sessions on one row, the old application after the retire); thirteen kinds of
dependency on the old column (index, partial index, unique and check constraints, a foreign key from another table,
a view, a materialized view, a row-level-security policy, a generated column, a trigger with a column list, a trigger
whose body names the column, a function naming table and column, extended statistics) each listed by the evidence,
each blocking the plan and each making the revision refuse with `alembic_version`, the columns and the triggers
intact; the lock timeout; a backend terminated while waiting for the lock; the reconnecting generation; aged
evidence; the half-done downgrade; the queued downgrade; and the retry that succeeds once nothing stands in the way.

Known and left alone: an additive revision line for a defaulted column with a long name can exceed the generated
project's 120-column Ruff limit (v2.1 template; cosmetic, the project runs; not touched in this pass).

Validation of the acceptance pass: `tests/test_destructive_acceptance_v22_postgresql.py` 5 OK (112.9 s, 131 MB);
`tests/test_retire_contract_v22_postgresql.py` 3 OK (84.5 s); `tests/test_retire_contract_v22.py` 13 OK;
full suite 719 OK on 3.13 (169.2 s, 137 MB) and 719 OK on 3.9 (186.1 s, 186 MB); PostgreSQL regression `test_renames_v22_postgresql.py` 4 OK (45.5 s), `test_migrations_v21_postgresql.py` 4 OK (61.9 s), `test_acceptance_v21.py` 4 OK (83.4 s), `test_acceptance_v20.py` 3 OK (54.1 s), `test_design_v20_postgresql.py` 9 OK (98.6 s);
`scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate`, Ruff, `tests/test_docs_links.py`,
`git diff --check` and the secrets scan pass; the generation oracle is unchanged (fresh projects identical to the
published engine except `core/db.py`; evolution plans differ only in manifests, the phased example aside).

## Increment 3: Safe Three-Way Merge Integration

The pure merge of increment 0 is wired into the planning, verification and application of evolutions, as an
**opt-in per exact file**. By default a generated file edited by hand on an integration point still blocks an
evolution; a contract may now name the file under `files.merge` (evolution 1.5), with the transformation and an
ownership boundary, and the operator adds `--allow-merge` on the command line. With both halves present the plan
computes a three-way merge between the authenticated generated base, the developer's file and the regenerated file,
shows it, and only `apply` writes it, through the recoverable writer, after the merged project passed the
verification copy. The developer's code is never removed to make an evolution applicable; a conflict is a structured
record and a blocked plan, never a marker in a file. The decision record is
[`docs/architecture/adr-2.2-05-three-way-merge.md`](../architecture/adr-2.2-05-three-way-merge.md); the guide is
section 10 of [`docs/guides/evolving-generated-projects.md`](../guides/evolving-generated-projects.md); the contract is
section 12 of `references/evolution-contracts.md`.

### Design

- **Authorisation, exact and two-sided.** `files.merge[] {path, transform: three_way_python|three_way_json,
  ownership {generator_may_change: file|regions, developer_owns: regions, generator_owns: regions}, on_conflict:
  reject}`; patterns, directories, the manifest, the journal, historical revisions, preserved files and unknown keys
  are refused; `reject` is the only conflict behaviour that exists. `--allow-merge` is the second half; the flag
  alone merges nothing, the contract alone blocks with the flag named.
- **BASE authenticated by digest.** The base is regenerated from the contract the manifest records and accepted only
  when its digest equals the digest the manifest recorded for the file (`plan.merges[].base`); otherwise
  `merge_base_not_reconstructible` with both digests, `merge_base_unknown` for a file the manifest does not know, and
  the existing `baseline_contract_available` for a manifest whose contract is not the one its digest names. LOCAL is
  read with its line endings; NEW is the generator's output. The three digests travel in the plan, the journal's
  `merge` step and the report.
- **Ownership boundary.** Regions are top-level functions (or `Class.method`), classes or line ranges of the
  generated base: a manual change inside a generator-owned region, a generated change inside a developer-owned one, or
  outside the authorised regions, is a named conflict; everything else merges hunk by hunk.
- **Accepted / refused.** Non-overlapping hunks with both sides' nodes present (the structural proof of `merge.py`),
  headers of a definition changed by at most one side, no duplicate symbol, import, import binding or router
  registration under the same prefix, every comment and hunk of the developer present, both sides and the result
  parsing, line endings unchanged. Refused as records with base lines and both sides' text: `overlapping_changes`,
  `insertions_at_the_same_point`, `structural_proof_failed`, `signature_or_decorators_changed_by_both_sides`,
  `duplicate_addition`, `duplicate_symbol`, `duplicate_import`, `conflicting_import`,
  `duplicate_router_registration`, `manual_comment_lost`, `manual_change_lost`, `line_endings_changed`,
  `local_does_not_parse`, `generated_does_not_parse`, `json_value_conflict`, the ownership and base conflicts.
- **Plan, apply, verify.** The plan is the dry run (digests, hunks taken from each side, preserved manual hunks, the
  diff from LOCAL to the result, the conflicts, the authorisation required, the validations the copy will run). Apply
  publishes the planned text, never a recomputed one: the writer's digest preconditions refuse a LOCAL changed after
  the plan, a re-check of the planned result digest refuses a plan altered after verification, the project lock and
  the journal (`merge` step) apply as always and `recover` restores the developer's file after an interrupted
  publication. The verification copy re-checks the merged file (planned digest, parse, duplicates, developer's hunks
  and comments) before the audit, the route contracts, the protected files and the authorised tests; `verified` means
  the merged project passed them, not that the merge is semantically right beyond what they see. The manifest keeps
  the generated digest of a merged file (the manual hunks stay the developer's and merge again next time) and
  `history[].merged` names the files (manifest 1.6).
- **A defect found on the way.** A verification copy inherited the project's lock file; with another process holding
  the lock, `apply` crashed with `LockHeld` from inside the copy instead of ending in `applied.state: locked`. The
  copy no longer carries the lock, and `LockHeld` inside the copy is a rejection reason.

### Contracts and schemas (all additive)

| Artefact | Change |
|---|---|
| `fastapi-architect/evolution/1.5` | `files.merge[] {path, transform, ownership, on_conflict}`; `rename_fields`, `retire_fields` and `authorization.destructive` accepted under 1.5 too; 1.0–1.4 keep their meaning |
| `fastapi-architect/generation/1.6` | `history[].merged`; 1.0–1.5 still read |
| `evolution_report/1.6` | `plan.merges[]`, `plan.files.merged[]`, `verification.merges[]`, `contract_authorization.allow_merge` |
| journal | step kind `merge` (paths and the base, local, new and result digests) |
| CLI | `evolve_fastapi.py plan|apply --allow-merge` |
| Example | `examples/evolutions/team-notes-add-reminders-merge.json` |

### Deliberate differences from v2.1.0 and from the earlier increments

- Projects whose contracts have no `files.merge` behave exactly as before: the oracle against the published engine is
  unchanged (fresh projects identical except `core/db.py` of the PostgreSQL ones; evolution plans differ only in their
  manifests, the phased example aside). The five committed snapshots changed their manifests only (1.6).
- The discovery proposed `files.allow_merge` with globs; the implementation requires exact paths, a transformation and
  an ownership boundary (ADR-2.2-05 records why).

### Validation

| Check | Result |
|---|---|
| `tests/test_merge_integration_v22.py` | 14 OK on 3.13 (18.8 s) and 13 OK + 1 skipped on 3.9 (the end-to-end flow with the generated suite needs an interpreter that imports FastAPI): the authorisation refused by name (schema gate, globs, directories, escapes, the manifest and revisions, duplicates, transforms, `on_conflict`, ownership shapes, regions, unknown keys, preserve contradiction), the default still a refusal and both halves required; scenario A (import, middleware, decorated helper with comments preserved; apply; `history[].merged`; journal `merge` step; repeat as `noop`; a later evolution merging the same hunks again), B (same insertion point: `insertions_at_the_same_point` with both sides' text), C (manual keys of the audit configuration preserved; three-way JSON merge and conflict on synthetic documents), D (comments, decorators, imports, a positional-only and keyword-only signature), E (`manual_change_in_generator_region`, `generated_change_in_developer_region`, `generated_change_outside_authorized_region`, `merge_region_unresolved`); the whole flow plan → verify (generated suite through the trust model, `verification.merges` ok) → apply → repeat `noop`; adversarial: base absent, corrupt and from another contract; LOCAL changed after the plan and NEW changed after verification refused by the writer with the tree untouched; signature, decorator, duplicate-import, conflicting-import and duplicate-route conflicts named; CRLF (`line_endings_changed`), a tab-indented line (`local_does_not_parse`), a tab inside a comment preserved, a lost comment; a symlink (`unsafe_path`), a renamed target, a lock held by another process (`locked`), an interrupted journal, rollback restoring the developer's version, the retry applied, the repetition `noop`; the command line planning without writing |
| Existing behaviour | full suite 733 OK on 3.13 (136.0 s, 136 MB) and 733 OK on 3.9 (141.7 s, 183 MB); PostgreSQL regression (the evolution paths touch every PostgreSQL scenario): `test_renames_v22_postgresql.py` 4 OK (47.6 s), `test_retire_contract_v22_postgresql.py` 3 OK (77.3 s), `test_migrations_v21_postgresql.py` 4 OK (58.1 s), `test_acceptance_v21.py` 4 OK (56.0 s), `test_acceptance_v20.py` 3 OK (40.8 s), `test_design_v20_postgresql.py` 9 OK (65.9 s); no Docker executor path was modified, so no real container run was repeated |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate`, Ruff, `tests/test_docs_links.py`, `git diff --check`, secrets scan | pass; the Docker executor was not modified (its tests kept their place in the suite; no real container run) |
