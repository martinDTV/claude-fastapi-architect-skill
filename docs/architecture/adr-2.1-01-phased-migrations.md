# ADR 2.1-01 — Phased migrations: expand, backfill, verify, contract

Status: accepted (2026-10-31, decisions of the v2.1.0 review) and implemented in increment 2. Companion of
ADR 2.1-02 (renames with dual-write), which stays deferred.

## Context

v2.0 evolutions produce exactly one additive Alembic revision applied in one transaction. A column that existing
rows cannot satisfy was therefore either refused (`backfill_strategy`) or filled by a server default at DDL time,
which rewrites the table under an exclusive lock when the default is volatile and leaves no room for a value computed
per row. Nothing modelled a change that spans deployments, and nothing distinguished "the revision file exists"
from "this database applied it" and from "this database was checked".

## Decision

1. **Phases are revisions; the backfill is a program the project ships.** An expand evolution appends a
   transactional revision (nullable column, no default, the progress table once) and, for concurrent indexes, a
   second non-transactional revision; a contract evolution appends a revision that performs the NOT NULL transition
   online. Between them, `src/<pkg>/ops/backfill.py` (generated, standard library plus the project's SQLAlchemy and
   asyncpg) fills the rows in keyset batches with the checkpoint in the same transaction as the data. The plugin
   itself never connects to a database.
2. **Declared, applied, verified.** The manifest (`generation/1.3`) records what was declared
   (`phased_changes[].state_declared`, revisions, operation ids, the application version that writes the column).
   The database holds what was applied. `verify` writes what was measured (`migration-evidence/1.0`). A contract
   plan needs the third, fresh, about the right operation, head and application version; the contract revision
   re-checks the first two on the live database when it runs.
3. **Declarative backfills only.** Four strategies (`constant`, `copy_column`, `uuid`, `now`); identifiers come from
   names the contract validated; values are typed literals bound as parameters. No SQL travels from the contract.
4. **Online, not lock-free.** Expand adds the column without a rewrite; the index is built `CONCURRENTLY`; the
   contract uses `CHECK ... NOT VALID` + `VALIDATE CONSTRAINT` + `SET NOT NULL` (PostgreSQL 12+ skips the second
   scan) in autocommit mode, each step idempotent. Brief ACCESS EXCLUSIVE locks remain at the metadata steps and are
   printed as the lock profile of each phase.
5. **Concurrency semantics stated, not assumed.** `FOR UPDATE SKIP LOCKED`, `WHERE column IS NULL` at update
   time, a final count per pass and up to three passes. The guarantee is "never overwrite a value, never wait for
   the application, `complete` means zero pending rows observed"; the residual (an old application version still
   inserting NULLs) is caught by the contract revision and must be excluded by the operator's declaration of the
   deployed version; a later run re-counts a complete operation and reopens it for exactly the rows that appeared. The runner refuses RLS tables unless the role bypasses RLS instead of silently seeing nothing.
6. **Contract is a human decision.** `--authorize-contract` on the command line and `authorization.contract.declared_by`
   in the contract, plus evidence no older than the configured age; a contract never runs as the continuation of a
   backfill, and v2.1 contracts drop nothing (only nullability and the transitional constraint). Reversible by
   `DROP NOT NULL`.
7. **Concurrent indexes verified, not assumed.** The index revision compares `pg_get_indexdef` with the expected
   definition, drops an invalid leftover and rebuilds it, and fails if `indisvalid` is false when it ends.
8. **Compatibility.** Contracts 1.0 and 1.1 keep their meaning; 1.2 adds `backfill`, `add_indexes`,
   `contract_fields` and `authorization` (refused under older schemas with the path). Manifests 1.0 to 1.2 are still
   read; 1.3 adds `phased_changes`, `concurrent_indexes` and a `phase` per revision. Existing projects without phased
   changes generate byte-identical code; only their manifests gain the new keys.

## Alternatives rejected

- One revision with `autocommit_block` for everything: the backfill would run inside a migration with no
  checkpoint, no resumption and no way to stop; a failure midway would leave no record.
- Triggers for dual-write: invisible to the auditor and to the generated tests; deferred with renames.
- `OFFSET` pagination: skips or repeats rows while the table changes; keyset on the primary key with a final count
  has provable invariants.
- Trusting `IF NOT EXISTS` for indexes: it says nothing about the definition or the validity of what exists.
- Letting the plan infer that a backfill completed from repository state: the state lives in the database; the
  evidence is the only input the plan accepts, and the revision checks again.

## Consequences

- Generated projects with phased changes carry an `ops` package, a progress table in their metadata and a
  `tests/test_backfill.py`; their suites prove the runner's behaviour on the empty verification database.
- Operators run three commands between the two evolutions (`alembic upgrade head`, `backfill run`, `backfill
  verify`) and pass the evidence to the contract plan. The guide documents the sequence and the lock profile.
- A fresh environment can apply the whole chain: the contract revision treats an empty table with no backfill
  record as complete and records the fact with a sentinel checksum the runner and the evidence recognise (a later
  `run` has nothing to do; `verify` is `ok`).
- Deferred: renames with dual-write, drops of deprecated columns with evidence, multi-column and partial indexes,
  per-tenant backfills under RLS as the application role, backfills with expressions beyond the four strategies.
