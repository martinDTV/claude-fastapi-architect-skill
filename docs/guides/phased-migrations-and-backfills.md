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
