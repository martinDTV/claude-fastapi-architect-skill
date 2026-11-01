# ADR 2.2-01 — Safe column renames: expand, transition, retire, contract

Status: accepted (2026-10-31, decisions of the v2.2.0 review); expand and transition implemented in increment 1,
retire and the destructive contract in increment 2 (2026-11-01). The only destructive operation of the tool is the
contract of a retired rename, and it is disabled unless everything in the second half of this record holds.

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
| C (new) | the new name | the new column; the trigger fills the old one until contract | the new column |

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
   column changed the new one follows, else if the new one changed the old one follows. **A statement that sets both
   names to different values is refused** (`RAISE EXCEPTION`, SQLSTATE 23514, the writer's transaction rolls back):
   the trigger cannot read the writer's intent from `OLD`/`NEW` in that case, so it does not guess (increment 2
   replaced the "old column wins" rule of increment 1 before anything shipped). There is no exception: a column
   default would make an INSERT through one name indistinguishable from an INSERT through both, so the acceptance of
   increment 2 refuses renames of fields that declare a default (`rename_defaulted_column_unsupported`) after a test
   showed the default being taken for a value. The function only assigns to `NEW`:
   it never issues another statement, so it cannot recurse; it runs inside the writer's statement and transaction, so
   a rollback covers both columns; it is `SECURITY INVOKER`, so it carries no privilege; row-level security policies
   still check the final row. It is named after the operation (`trg_<table>_<op>_sync`, `fn_<table>_<op>_sync`), its
   comment records `op=<id> truth=<column>` (and `retired_at` after the retire), it is recorded in the manifest and
   the runner's definitions, reported by the evidence (present, enabled, truth, retired_at) and exercised by the
   generated tests. The expand revision's downgrade drops the trigger, the function and the new column: no data is
   lost because the old column holds it all. The transitional application refuses, at its own boundary (422), a
   request that sends both names of a field, equal or not: one name per request, so no precedence is ever applied to
   input.
4. **Visibility.** The trigger is generated code in an Alembic revision the auditor reads (its migration rules report
   nothing for it: 0 findings before and after on the fifth example, measured), it appears in the plan's `renames` and
   `transition` phase with its policies, in the manifest and in the runner's definitions, the generated tests exercise
   it in both directions, and `verify` fails when it is missing or disabled. "Invisible" no longer applies.
5. **What the plan refuses**: `in_place`, any sync other than `trigger`, a source that does not exist, a target that
   exists, the identity, ownership, tenant and timestamp columns, foreign keys, a column already in transition, a
   column covered by a concurrent index, a rename on an in-memory project or a new table, and the contract of a
   rename that was not retired first. A repeated rename is a no-op.
6. **Cross-version compatibility, by pair**: A/B compatible on the expanded schema (demonstrated with two processes
   from two environments against one database); B/C compatible on the retire schema (demonstrated the same way); A
   keeps working on the retire schema through the trigger but is outside its contract; A and B are incompatible with
   the contract schema (B maps the dropped column and fails, demonstrated), and the plan identifies both before the
   drop through the sessions of the evidence.
7. **Retire moves the truth, drops nothing.** A second evolution (`retire_fields`, contract 1.4) needs fresh evidence
   from B's runner (trigger enabled, zero rows where the columns differ, backfill complete, the transitional version
   declared) and an identity (`authorization.contract.declared_by`). It generates the code of C (the fresh code of
   the contract under the new name: no alias, no dual write) and a `retire` revision that re-checks the database,
   swaps the sync function under the same trigger, carries the old column's NOT NULL (online) and default to the new
   column and stamps `truth=<new> retired_at=<now>` in the function comment. The old column stays for B. The
   downgrade moves the truth back; no data moves either way. There is no command-line flag: the step is reversible.
8. **The destructive contract is exceptional, explicit, verified and disabled by default.** A third evolution
   (`contract_fields` on a retired rename) drops the old column, its trigger and its function, `RESTRICT`, in one
   transaction under `ACCESS EXCLUSIVE` with `lock_timeout = 5s`, after sixteen named preconditions evaluated by the
   plan from the evidence of C's runner and re-checked by the revision on the live catalog: the operation's identity;
   retire declared and applied (the truth marker); evidence fresh and newer than the retire; zero differing rows;
   backfill complete; the retired version declared; every connected session identified as a generation that does not
   map the old column (the application tag, below), the runner's own or an ignored name, plus the operator's explicit
   declaration that older versions stopped; the configurable minimum age since the retire (default 24 h, never
   sufficient on its own); NOT NULL, type and default carried; no dependent in `pg_depend` beyond the column's own
   default and NOT NULL, no trigger function or user function naming the column unless reviewed by name; one Alembic
   head on disk and in the database; no managed file edited by hand; `--authorize-contract` with `declared_by`; the
   approver's identity; `--confirm-irreversible` with the exact phrase the plan prints (`drop <table>.<column>`),
   distinct from the authorization and never implied by it; no lock held and no interrupted operation. `CASCADE` is
   never generated; a dependent the checks missed fails the statement and rolls everything back. The invariant
   `no_destructive_schema_change` must be left out of that contract on purpose. The revision repeats, on the database
   clock and under the lock, what time can change after the plan: the connected sessions, the age of the evidence the
   plan read and the age of the retire (the acceptance showed a generation reconnecting between the plan and the
   upgrade, and evidence ageing past the contract's limit, both dropping the column before this). The downgrade fails
   after the same lock timeout rather than queue behind traffic.
9. **Reversibility, kept apart.** DDL reversibility of the drop is *conditional*: the downgrade recreates the column,
   copies the new column's current values back, restores NOT NULL and default and recreates the trigger, offline. Data
   recovery of the dropped content is not provided by the tool (a backup taken before the upgrade is the only way to
   the historical content). Compatibility restoration means stop, downgrade, redeploy B. There is no online rollback
   after a destructive contract, and the tool never says otherwise.
10. **Generations identify themselves.** Every generated application announces `application_name =
    <package>@<12 hex>` to PostgreSQL, a hash of the declared phased states, so B and C carry different tags; `verify`
    lists the sessions; the drop refuses while an unidentified session (A, or an external client not named in
    `ignore_sessions`) or a B tag is connected. A snapshot of `pg_stat_activity` cannot see instances that connect
    intermittently; that is why the declaration, the age and the sessions are all required and none is proof alone.

## Alternatives rejected

- `ALTER TABLE … RENAME COLUMN`: instant, but code and schema must switch together; refused by the review.
- "Old column wins" when one statement sets both names (increment 1): silent loss of one of the two values; replaced by
  a refusal in increment 2, with the single documented exception of defaults on INSERT.
- `DROP COLUMN … CASCADE`: would take views, policies and foreign keys with it; refused.
- Inferring that older instances are gone from the time elapsed since the retire: a wait is a policy, not evidence;
  kept as a configurable minimum but never sufficient.
- Trusting the operator's declaration of deployments alone: necessary, never sufficient; the sessions are checked.
- Application-level dual write only (B writes both, no trigger): A's updates leave the new column stale; the mirror
  is never a stable invariant and B could not read the new column safely until A is gone.
- A stored generated column: one-way, read-only, cannot carry the transition to C.
- An ORM synonym: no second column, hence no coexistence window.
- Bidirectional "newest wins" by timestamp: no per-column timestamp exists; the truth is a declared phase, not a clock.

## Consequences

- The evolution contract is `fastapi-architect/evolution/1.3` (`rename_fields`) and `1.4` (`retire_fields`,
  `contract_fields` on a retired rename, `authorization.destructive`), the manifest 1.4 (`kind`, `from_column`,
  `from_field`, `sync`, `source_of_truth`, `read_policy`, `write_policy`, `transition`) and 1.5 (`application_tag`,
  the retire and contract states, `compatible_tags`, `dropped`, `destructive`), the evidence 1.1 (`mismatch_rows`,
  `columns_agree`, `sync`) and 1.2 (`columns`, `sync.truth`, `sync.retired_at`, `dependents`, `other_triggers`,
  `functions_mentioning`, `sessions`), the report 1.4 (`plan.migration.renames`, the `transition` phase) and 1.5
  (`retires`, the `retire` and destructive `contract` phases, `confirm_irreversible`).
- Operators run, after the expand evolution: `alembic upgrade head` (new column and trigger), the backfill of each
  rename as the owner, `verify`; the old version may keep running the whole time.
- Hand-written SQL that names the old column keeps working; hand-written SQL that expects the new column to be filled
  must wait for the backfill. The plan says so.
- Operators run, after the retire evolution: `alembic upgrade head` (the retire revision), deploy C, stop A and B,
  `verify` with C's runner; after the destructive contract: a backup if the historical content matters, `alembic
  upgrade head`; the downgrade of the drop is an offline, conditional recovery.
- The plan states what is known about deployments in five parts (human declaration, observed sessions, schema state,
  automated verification, residual uncertainty: `plan.migration.phases[contract].assurance`), and the evidence carries
  its `limits`; none of the parts is presented as proof that older instances are gone.
- Still unavailable: renames of columns covered by a concurrent index or declaring a default (refused), in-place
  renames, type changes,
  removal of entities, and any proof of a distributed deployment beyond what one database can observe.
