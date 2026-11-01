# Phased migrations and safe backfills

v2.1 lets a generated PostgreSQL project change a schema that existing rows cannot satisfy without a maintenance
window: a column is added **nullable** (expand), existing rows are filled by a **resumable backfill** that the
project ships, the state of the target database is **verified** and written down as evidence, and only then, with
an explicit human authorization, the column becomes **NOT NULL** (contract). Concurrent indexes on existing tables
follow the same discipline. The fifth example, `parcels`, shows every step; `tests/test_migrations_v21_postgresql.py`
runs them against a real cluster.

Three states are never confused:

| State | Where it lives | Who establishes it |
|---|---|---|
| **declared** | the evolution contract and the manifest (`phased_changes[].state_declared`: `expand` or `contract`) | `evolve_fastapi.py plan` / `apply` |
| **applied** | a concrete database: `alembic_version`, the column's nullability, the progress table | `alembic upgrade head`, `python -m <pkg>.ops.backfill run` |
| **verified** | the migration evidence written for that database at a time | `python -m <pkg>.ops.backfill verify` |

The existence of a revision file in the repository says nothing about any database. The plugin never connects to
a database; it reads the evidence.

## Phase 1: expand

An evolution contract 1.2 declares a field with a `backfill`:

```json
{"name": "carrier", "type": "str", "max_length": 30, "required": true,
 "backfill": {"strategy": "constant", "value": "unknown"}}
```

Strategies: `constant` (a typed literal, bound as a parameter), `copy_column` (another column of the same table and
type), `uuid` (`gen_random_uuid()`, for `uuid` fields), `now` (for `datetime` fields). Nothing else: the contract
never carries SQL, and the planner refuses `default` or `references` next to a `backfill`. The same entity may
declare `add_indexes` (`{"columns": ["carrier"], "unique": false}`, one column per index in this version).

The plan (`evolve_fastapi.py plan EXPAND.json`) produces:

- an **expand revision**, transactional: `ADD COLUMN ... nullable` without a server default (a brief ACCESS
  EXCLUSIVE lock, no table rewrite) and, the first time, the progress table `<pkg>_backfill_progress`;
- an **index revision**, non-transactional (`autocommit_block`): `CREATE INDEX CONCURRENTLY` (SHARE UPDATE
  EXCLUSIVE: writes continue; two scans; waits for transactions that started before it). It does not trust
  `IF NOT EXISTS`: an index with the same name and another definition is a conflict and the revision fails; an
  invalid leftover of an interrupted build is dropped and rebuilt; a valid identical index is kept; the revision
  fails rather than end with an invalid index;
- the model with the column nullable (`Mapped[str | None]`), the create schema requiring it (new rows always
  carry a value), the read schema optional (existing rows read NULL until the backfill completes);
- `src/<pkg>/ops/backfill.py` (the runner), `src/<pkg>/ops/backfills.py` (the declarative definitions, operation
  ids, checksums, expected head) and `tests/test_backfill.py`;
- a stable **operation id** per phased change (sha1 of table, column, strategy, parameters and the head it was
  declared on) and the **application version** that writes the column (the evolved contract digest), both printed
  in `plan.migration.backfills` and recorded in the manifest (`fastapi-architect/generation/1.3`).

`apply` verifies the evolved project in a copy as always (audit, routes, protected files, and the generated suite
through the trust model, `tests/test_backfill.py` included), then writes the project. Nothing has reached a
database yet.

## Phase 2: backfill

```
alembic upgrade head                                            # expand + index revisions on the target database
python -m parcels.ops.backfill run 66636e4277641692 --batch-size 500      # as the owner; resumable
python -m parcels.ops.backfill status 66636e4277641692
```

Credentials come from `<PKG>_MIGRATIONS_DATABASE_URL` (the owner) or `<PKG>_DATABASE_URL`; nothing is embedded.

The algorithm:

1. one advisory lock per operation (`pg_try_advisory_lock`) plus a worker record: a second worker exits with code 3
   and changes nothing;
2. the progress row is created once (`INSERT ... ON CONFLICT DO NOTHING`); its checksum of the definition (table,
   column, type, strategy, parameters) is compared with the current one: a checkpoint that belongs to another
   definition, or whose key is not a primary key, is refused (code 4) without touching data;
3. a table with row-level security is refused (code 5) unless the role bypasses RLS (owner or `BYPASSRLS`): as the
   application role the backfill would silently see no rows;
4. **keyset batches**: `SELECT id ... WHERE column IS NULL AND id > :last_key ORDER BY id LIMIT :n FOR UPDATE SKIP
   LOCKED`, then `UPDATE ... SET column = <expression> WHERE id = ANY(:ids) AND column IS NULL`, then the
   checkpoint (`last_key`, `rows_processed`, `batches`), **all three in one transaction**. A crash before the commit
   loses nothing; a crash after it resumes from the committed checkpoint. OFFSET is never used;
5. `--max-batches`, `--max-seconds` and `--batch-pause-ms` bound a run; the state stays `running` and the next run
   resumes;
6. when a pass finds no more keys, the runner **counts** the pending rows. Zero means `complete`. Otherwise (rows
   inserted during the pass by an application version that does not write the column, or rows that were locked and
   skipped) a new pass starts from the beginning, up to three; after that the state is `incomplete` (code 6) and the
   count is reported.

What this guarantees, and what it does not:

- a value the application wrote is **never overwritten** (`WHERE column IS NULL` at update time, under the row
  lock);
- rows the application holds locked are **not waited for**; they are revisited in the next pass;
- `complete` means **zero pending rows observed** after the last pass, not that no NULL can appear later. While
  an application version that does not write the column is still running, new NULLs can appear; the contract
  revision re-checks the database and refuses in that case, and running the backfill again re-counts the complete
  operation and reopens it for exactly those rows (a run that finds nothing processes nothing). Accrediting that the
  writing version is the only one running is the operator's declaration (below);
- online safety is claimed for **filling NULLs of a new column**; it is not a general data migration engine.

## Phase 3: verify

```
python -m parcels.ops.backfill verify --application-version <digest from the plan> --output evidence.json
```

The evidence (`fastapi-architect/migration-evidence/1.0`) records: the database (name, user, server version,
whether the role bypasses RLS), `alembic_version` and whether it is the expected head, and for every phased change
the column's presence and nullability, the pending rows, the progress record (state, rows, batches, passes, last
key, checksum) and whether the backfill is complete; for every concurrent index its presence, validity and
definition; the application version the operator declares; `ok` and the list of problems. It is a measurement at a
time: the plan rejects it after `max_evidence_age_hours` (24 by default).

## Phase 4: contract

A second evolution contract names the operations to contract and the authorization:

```json
{"contract_fields": [{"name": "carrier", "operation": "66636e4277641692"}]}
...
"authorization": {"contract": {"declared_by": "release manager", "evidence": "evidence.json", "max_evidence_age_hours": 24}}
```

`evolve_fastapi.py plan|apply CONTRACT.json --authorize-contract [--evidence FILE]`. Every precondition is a named
check in the plan and blocks it when unsatisfied: `contract_authorized` (the flag **and** `declared_by`: a contract
is a human decision, never the continuation of a backfill), `contract_operation_pending` (declared `expand` in the
manifest), `contract_evidence_present`/`_valid`/`_fresh`, `contract_database_at_head`, `contract_evidence_matches`,
`contract_column_present`, `contract_backfill_complete`, `contract_no_pending_rows`,
`contract_application_version_accredited` (the evidence declares the version the plan printed),
`contract_indexes_valid`, `contract_reversible`. The committed sample evidence of the example is deliberately stale,
so planning the example's contract from the repository alone is refused.

The contract revision runs statement by statement in autocommit mode so the validation scan does not hold the
exclusive lock: it re-checks pending rows and the progress state on the live database (an empty table in a fresh
environment counts as complete and records the fact), then `ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID`
(brief ACCESS EXCLUSIVE, no scan), `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE, writes continue), `ALTER COLUMN
... SET NOT NULL` (PostgreSQL 12+ uses the validated constraint: no second scan; still a brief ACCESS EXCLUSIVE),
`DROP CONSTRAINT`. Each step checks the catalog first, so a re-run after an interruption continues. The downgrade
is `DROP NOT NULL`: no data is lost. The model and the read schema become non-optional; the manifest records
`state_declared: contract`.

None of this is lock-free. The table receives brief exclusive locks at the metadata steps; the long scans
(backfill, index build, validation) run with locks that allow writes. The plan prints the lock profile of every
phase.

## Renames in transition (v2.2, increment 1)

A field rename is the same discipline with a second column and three application versions in play: **A** knows only
the old name, **B** knows both, **C** knows only the new one. Increment 1 delivers the expand evolution and the
transition between A and B; increment 2 (next section) the retire evolution (C) and the destructive contract that
drops the old column.

```json
{"rename_fields": [{"from": "reference", "to": "label", "strategy": "expand_contract"}]}
```

What `evolve_fastapi.py apply` produces from the fifth example (`parcels-rename-label.json` renames `reference` to
`label` and `weight_kg` to `weight`):

- an expand revision that adds `label` and `weight` nullable next to the old columns and creates one **sync trigger**
  per rename: `BEFORE INSERT OR UPDATE FOR EACH ROW`; whichever column a writer touched, the other follows inside the
  same statement and transaction; one statement that sets both to different values is refused (SQLSTATE 23514; the
  writer's transaction rolls back), with no exception: a field that declares a default cannot be renamed this way
  (`rename_defaulted_column_unsupported`), because the trigger could not tell its default from a value. The trigger
  only assigns to `NEW` (it cannot recurse), runs as the writer
  and leaves row-level security to the policies; its comment records `op=<id> truth=<column>`. The downgrade drops
  trigger, function and new columns without losing data: the old columns hold everything;
- the transitional code (B): the model maps both columns (`label` nullable, `reference` as before and marked as the
  source of truth), the request schemas accept either name (`AliasChoices`) and refuse a request that sends both (422,
  equal or not: the boundary never resolves an ambiguity by precedence), the read schema fills `label` from
  `reference` and still exposes `reference` as deprecated, the service writes both on create and update, and the
  module tests cover both names and the refusal;
- a phased operation of strategy `mirror_column` per rename (`python -m parcels.ops.backfill run <op>`): the v2.1
  runner with the predicate `label IS DISTINCT FROM reference`, so it reconciles the rows written **before** the
  trigger existed (and any drift injected behind a disabled trigger) and nothing else; its evidence adds
  `mismatch_rows`, `columns_agree` and the trigger state (missing or disabled is a problem).

Policies, stated once: the **source of truth** is the old column until the rename is retired; **B reads** the old
column; **B writes** both; **A writes** the old one; the trigger mirrors either write; **A and B coexist** on the
expanded schema; **C arrives with the retire evolution**. `complete` keeps its meaning (zero rows where the columns
differ at the time of the count) and a later run reopens the operation only for rows that differ.

Operations, in order: `apply` the rename evolution, `alembic upgrade head` on every database, keep A running if you
need to, run the backfill of each rename as the owner, `verify`. Deploy B whenever you like: before the backfill it
reads the old column and is correct; after it, the new column is equal to the old one on every row written through
the table.

Refused: `in_place`, a sync other than the trigger, renames of `id`, `owner_id`, `created_at`, the tenant column and
foreign keys, a target that exists, a column already in transition, a column covered by a concurrent index, renames on
in-memory projects, and the contract of a rename that was not retired first. Hand-written SQL that names the old column
keeps working; hand-written SQL that expects the new column to be filled must wait for the backfill.

Verified on this project's machine with `FA_DESIGN_PG=1 tests/test_renames_v22_postgresql.py`: the committed
`parcels` snapshot (A) and the evolved project (B) run as two processes from their own environments against one
expanded database with live rows; A's inserts and updates are mirrored, B reads A's rows before the backfill, B writes
both columns, A reads B's rows, interleaved updates of one row from both processes leave the pair equal, a rolled-back
write touches nothing, the backfill is killed and resumed while A keeps inserting, locked rows are skipped and
revisited, drift behind a disabled trigger is named by `verify` and fixed by a run, a disabled trigger is a problem,
a hand-made column of the new name makes the expand revision fail with the database unchanged, and a rename on a
row-level-security table mirrors the application role's writes. The numbers are in
`docs/releases/RELEASE-NOTES-v2.2.md`. Not demonstrated: a real distributed deployment; the two drivers are two
processes on one machine sharing one database.

## Retire and the destructive contract (v2.2, increment 2)

Two more evolutions finish the rename. Both read the **migration evidence** (`verify`), both are planned by
`evolve_fastapi.py` like any other evolution, and only the last one drops anything.

### Retire: the truth moves to the new column

```json
{"schema": "fastapi-architect/evolution/1.4",
 "authorization": {"contract": {"declared_by": "release owner", "evidence": "evidence-b.json"}},
 "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
    "retire_fields": [{"name": "label", "operation": "550095ef4c391dd0"}, {"name": "weight", "operation": "a8d969447e81ec47"}]}]}]}
```

Preconditions, from B's evidence: the trigger present and enabled, its truth marker on the old column, zero rows where
the columns differ, backfill complete, the transitional version declared (`verify --application-version <B digest>`),
evidence fresh and at the manifest head, `declared_by` present. No command-line flag: the step is reversible and online.

What it produces: the code of **C** (the fresh code of the contract under the new name: `label` and `weight` only, no
alias, no dual write, no deprecated field) and a `retire` revision that, statement by statement, re-checks the trigger
and the equality on the live database, replaces the sync function under the same trigger, carries the old column's NOT
NULL to the new one online (`NOT VALID`, `VALIDATE`, `SET NOT NULL`) and its default, and stamps the function comment
`truth=label retired_at=<now>`. Nothing is dropped: `reference` and `weight_kg` stay, equal to the new columns by the
trigger, for B. The manifest records `state_declared: retire`, the policies on `to_column` and `compatible_tags`.

Policies, stated once more: the **source of truth** is the new column from the retire revision on; **C reads** the new
column and **writes** it (the trigger fills the old one); **B reads** the old one (equal) and **writes** both; **A's
writes** to the old column are still mirrored (nothing is lost) but A is outside this phase's contract and must be gone
before the contract; **B and C coexist** on the retire schema. Downgrade of the retire revision: `DROP NOT NULL`,
`DROP DEFAULT`, the comment back to the old column; no data moves.

Operations: `apply` the retire evolution, `alembic upgrade head`, deploy C, retire A and B, `verify` with C's runner
(`verify --application-version <C digest>`): the evidence now lists the **sessions** of the database by
`application_name` (every generated application announces `<package>@<12 hex>`, a hash of its phased states; A, generated
before v2.2, announces nothing), the **truth marker** and `retired_at`, the **dependents** of the old column in the
catalog and every trigger or user function that names it.

### The destructive contract: the old column goes

```json
{"schema": "fastapi-architect/evolution/1.4",
 "authorization": {"contract": {"declared_by": "release owner", "evidence": "evidence-c.json"},
                   "destructive": {"approved_by": "release owner", "old_versions_stopped": true, "min_retire_age_hours": 24}},
 "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
    "contract_fields": [{"name": "label", "operation": "550095ef4c391dd0"}, {"name": "weight", "operation": "a8d969447e81ec47"}]}]}],
 "invariants": ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "protected_files_unchanged"]}
```

```
evolve_fastapi.py plan contract.json --authorize-contract --confirm-irreversible "drop shipments.reference, shipments.weight_kg"
```

The plan prints the phrase it expects when it is missing or wrong; the confirmation is distinct from
`--authorize-contract` and never implied by it. `no_destructive_schema_change` is left out of the invariants on
purpose; with it the plan blocks. The sixteen preconditions are listed in `references/evolution-contracts.md`, section
11; the ones operators meet most often: a session of A (unidentified) or of B (its tag) still connected
(`contract_deployments_identified`: stop them, or name an external client in `ignore_sessions`); the retire too recent
(`contract_retire_age`, configurable, never sufficient on its own); a view, an index, a policy or a foreign key on the old
column (`contract_dependencies_reviewed`: catalog dependents must go by hand; a user function that names the column can
be accepted by name in `reviewed_functions` after review); the evidence older than the retire or written by B's runner
(`contract_evidence_after_retire`, `contract_retire_applied`).

What it produces: a `drop` revision in **one transaction**: the connected sessions, the age of the evidence the plan
read and the age of the retire checked again on the database clock (time passes between the plan and `alembic
upgrade head`; a generation that reconnects, or evidence that aged past the contract's limit, makes the revision
refuse), then `lock_timeout = 5s`, `LOCK TABLE … ACCESS EXCLUSIVE`, every database-side condition re-checked on the
live catalog, `DROP TRIGGER`, `DROP FUNCTION`, `ALTER TABLE … DROP COLUMN … RESTRICT`. Never `CASCADE`: a dependent
the checks missed fails the statement and the whole revision rolls back, `alembic_version` included. The approver and
the phrase are written in the revision's docstring and in the manifest (`destructive {approved_by, declared_by,
confirmation, evidence_generated_at}`). What the plan knows about deployments is stated in five parts in
`plan.migration.phases[contract].assurance` (a human declaration, the sessions observed, the schema state measured, the
automated checks, and the residual uncertainty: an instance idle at both observations, a pooler hiding the application
name, code that names the column without connecting under its own name).

Reversibility, kept apart. **DDL**: conditional; the downgrade recreates the column, copies the new column's *current*
values back, restores NOT NULL and recreates the sync trigger (truth on the new column), offline: a full-table `UPDATE`
in one transaction that fails after the lock timeout rather than queue, and rolls back entirely on any error. **Data**:
the content the old column had at the drop is not recoverable from the
database (after the checks it equalled the new column, which stays); take a backup before the upgrade if it matters.
**Compatibility**: B works again after the downgrade; there is no online rollback: stop, downgrade, redeploy. C keeps
working across the drop because it never named the old column.

Verified on this project's machine with `FA_DESIGN_PG=1 tests/test_retire_contract_v22_postgresql.py` (A, B and C as
three processes from their own environments on one database): the numbers and the scenario list are in
`plugins/fastapi-architect/examples/generated/VERIFICATION.md` (section "v2.2 increment 2") and in
`docs/releases/RELEASE-NOTES-v2.2.md`. Not demonstrated: a distributed deployment; the sessions check is a snapshot and
cannot see instances that connect intermittently.

## What is deliberately not in v2.1

Renames (add-copy-drop with dual-write), drops of columns or tables, type changes, data migrations with business
logic, multi-column indexes, backfills of foreign keys, backfills as the application role under RLS (per-tenant
iteration), and anything that would need a trigger. A contract phase never drops anything.

## Verified on this project's machine

`FA_DESIGN_PG=1 python3 -m unittest discover -s tests -p test_migrations_v21_postgresql.py` runs the vertical
slice on a throwaway PostgreSQL 16 cluster: 2 500 existing rows, expand and index applied, a clean stop after three
batches, two workers (one exits locked), a SIGKILL in the middle of a run with the checkpoint and the rows found
equal afterwards, resumption to completion with every row filled exactly once, application inserts and updates
during the second backfill (values written by the application preserved; rows inserted after the last count left
NULL in both columns, found by a re-run that reopens the complete operations for exactly those rows), corrupt and
foreign checkpoints refused, the evidence, the contract refused without authorization and with the stale sample,
planned and applied with fresh evidence, refused by the revision itself when a NULL reappears, reversible by
downgrade, and the whole chain applied to a fresh database (the contract revision records the empty table; the
evidence is ok; a run has nothing to do); a conflicting index definition refused and
an invalid index rebuilt; a connection failure and a time limit that never report success; a row-level-security
table refused as the application role and filled as the owner. The measured times are in
`docs/releases/RELEASE-NOTES-v2.1.md`.
