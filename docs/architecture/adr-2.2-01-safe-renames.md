# ADR 2.2-01 — Safe column renames: expand, transition, retire, contract

Status: accepted (2026-10-31, decisions of the v2.2.0 review) and implemented for its first two states in increment 1
(expand and transition). Retire and contract are later increments; nothing in v2.2 increment 1 removes a name or
drops a column.

## Context

A field rename is the most requested change v2.1 could not express: `rename_fields` was refused, and the only way
out was a new field plus a hand-written migration. The review decided that renames are `expand_contract` only (no
`ALTER TABLE … RENAME COLUMN`: a metadata rename is instant but it is not online across application versions), that
the sequence is three mandatory evolutions (`expand`, `retire`, `contract`), that no destructive phase ever runs
automatically, that the previous version's behaviour must be demonstrated against the expanded schema (N-1), and that
the old version must not be assumed to perform a dual write.

That last point decides the synchronisation mechanism. Three logical versions share one database during a rename:

| Version | Knows | Writes | Reads |
|---|---|---|---|
| A (old) | the old column only | the old column | the old column |
| B (transitional) | both | both, in the same statement | the old column (the source of truth), exposing both names |
| C (new) | the new name | both until contract (retire keeps the mirror) | the new column |

A's writes reach only the old column. If nothing mirrors them, B's new column is stale for every row A touches, the
backfill has to be re-run forever and the equivalence of the two columns is never a stable property. The discovery
of v2.1 recommended application-level dual write and rejected triggers as invisible to the auditor and the tests;
that recommendation covered B and C, which the generator writes. It cannot cover A, whose code exists already.

## Decision

1. **A rename is a phased change with a mirror backfill.** The expand evolution adds the new column nullable, next
   to the old one, and declares a phased operation of strategy `mirror_column` (new := old wherever they differ,
   NULLs included). The runner of v2.1 is reused unchanged in its semantics: keyset batches, `FOR UPDATE SKIP
   LOCKED`, checkpoint and rows in one transaction, one worker per operation, `complete` only after a zero count,
   reopen for rows that appear later. The pending predicate of a rename is `new IS DISTINCT FROM old`.
2. **The old column is the source of truth until the rename is retired.** B reads it (a validation alias on the read
   schema), writes both in the same statement, accepts the old name on requests (an alias on the request schemas) and
   exposes both names on responses, the old one marked deprecated in the OpenAPI document.
3. **A scoped sync trigger keeps the pair equal for every write, whoever makes it.** The expand revision creates a
   `BEFORE INSERT OR UPDATE … FOR EACH ROW` trigger per rename with these rules: on insert, if the old column is not
   NULL the new one takes its value, else if the new one is not NULL the old one takes it; on update, if the old
   column changed the new one follows, else if the new one changed the old one follows. The old column wins when one
   statement sets both to different values. The function only assigns to `NEW`: it never issues another statement, so
   it cannot recurse; it runs inside the writer's statement and transaction, so a rollback covers both columns; it is
   `SECURITY INVOKER`, so it carries no privilege; row-level security policies still check the final row. It is named
   after the operation (`trg_<table>_<op>_sync`, `fn_<table>_<op>_sync`), recorded in the manifest and the runner's
   definitions, reported by the evidence (present, enabled) and exercised by the generated tests. The expand
   revision's downgrade drops the trigger, the function and the new column: no data is lost because the old column
   holds it all.
4. **Visibility.** The trigger is generated code in an Alembic revision the auditor reads (its migration rules report
   nothing for it: 0 findings before and after on the fifth example, measured), it appears in the plan's `renames` and
   `transition` phase with its policies, in the manifest and in the runner's definitions, the generated tests exercise
   it in both directions, and `verify` fails when it is missing or disabled. "Invisible" no longer applies.
5. **What the plan refuses**: `in_place`, any sync other than `trigger`, a source that does not exist, a target that
   exists, the identity, ownership, tenant and timestamp columns, foreign keys, a column already in transition, a
   rename on an in-memory project or a new table, and the retire or contract of a rename (not available in this
   version). A repeated rename is a no-op.
6. **Cross-version compatibility, by pair**: A/B compatible on the expanded schema (demonstrated with two processes
   from two environments against one database); B/C compatible later because C keeps writing both until contract;
   A/C incompatible (C relies on the new column being complete). C is defined by this record and not generated yet.

## Alternatives rejected

- `ALTER TABLE … RENAME COLUMN`: instant, but code and schema must switch together; refused by the review.
- Application-level dual write only (B writes both, no trigger): A's updates leave the new column stale; the mirror
  is never a stable invariant and B could not read the new column safely until A is gone.
- A stored generated column: one-way, read-only, cannot carry the transition to C.
- An ORM synonym: no second column, hence no coexistence window.
- Bidirectional "newest wins" by timestamp: no per-column timestamp exists; the truth is a declared phase, not a clock.

## Consequences

- The evolution contract is `fastapi-architect/evolution/1.3` (`rename_fields`), the manifest 1.4 (`kind`,
  `from_column`, `from_field`, `sync`, `source_of_truth`, `read_policy`, `write_policy`, `transition`), the evidence
  1.1 (`mismatch_rows`, `columns_agree`, `sync`), the report 1.4 (`plan.migration.renames`, the `transition` phase).
- Operators run, after the expand evolution: `alembic upgrade head` (new column and trigger), the backfill of each
  rename as the owner, `verify`; the old version may keep running the whole time.
- Hand-written SQL that names the old column keeps working; hand-written SQL that expects the new column to be filled
  must wait for the backfill. The plan says so.
- Still unavailable in this increment: the retire evolution (C: new name only in code, mirror kept) and the contract
  evolution (drop the old column and the trigger, with evidence newer than the retire, equality of the columns, a
  declared deployment compatibility, explicit human authorisation, a minimum wait that is never sufficient proof, and a
  downgrade that recreates the column and copies the data back).
