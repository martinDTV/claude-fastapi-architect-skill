# ADR 2.2-07 — Recovery and concurrency hardening: one writer, a validated journal, nothing of anyone else's overwritten

Status: accepted and implemented in increment 5 of v2.2.0 (2026-11-01). It hardens the recoverable writer of v2.0
(`journal.py`), the durability of v2.1 and the lock and journal 1.1 of v2.2 increment 0 without changing the file
protocol, the operation identity or the journal schema (`fastapi-architect/operation/1.0` and `1.1` are read and
recovered as before; 1.1 gains three optional keys).

## Context

Every path that writes a project goes through one mechanism: generation (since this increment), evolutions of
generated projects (regeneration, merge, phased migrations, renames, retire and contract), evolutions of adopted
projects (transforms, mount edits, generated modules) and adoption itself all end in `write_recoverable`; remediation
writes only in isolated copies and hands back diffs. The mechanism promised per-file atomicity, a journal, forward and
rollback recovery and durability. Increment 5 asked one question of it: can an interrupted, concurrent or rejected
operation silently overwrite somebody else's change, lose data, or be reported as done without evidence? The answer
was yes in a handful of places, listed under *Defects*.

## The state model

**Persisted** in `journal.json` (`state`): `prepared` (staged, nothing written), `publishing` (renames under way;
`published[]` names the files that landed), `published` (every file landed, cleanup pending), `recovering_rollback`
and `recovering_forward` (recovery under way; `recovered[]` names the files handled, `recovery_strategy` the strategy
it started with), `needs_attention` (recovery stopped at files it will not touch; `conflicts{}` says why) and
`completed` (written right before the cleanup, so an interrupted cleanup is finished by the next reader instead of
being mistaken for an interrupted publication).

**Inferred** from the tree, never written: *pending* (a journal exists), *interrupted* (a journal says `publishing`
or `published` and no process holds the lock), *conflict* (a file whose content is neither the expected nor the new
digest), *recovered* (the journal is gone after a recovery; the report says `rolled_back`, `completed` or
`discarded`), *locked* (the lock file exists), *unreadable* (a journal that does not validate), *stale*, *unknown*,
*invalid* (the lock's holder judged gone, unjudgeable, or the lock path not a regular file).

**Transitions and their preconditions**

| From | To | Precondition |
|---|---|---|
| — | `prepared` | the lock is ours; no other operation of the project is pending or unreadable; every path is safe and crosses no symlink; every expected digest and every guard holds; the staging and the journal are written and synced |
| `prepared` | `publishing` | the same preconditions re-checked |
| `publishing` | `publishing` (+1 file) | the file's current digest is still the expected one (or absent for a creation) and the staged copy matches the journal; otherwise the file is recorded as refused and the publication stops |
| `publishing` | `published` | every file landed |
| `published` | `completed` | — (the cleanup follows; a crash here is finished by the next reader) |
| `prepared` | discarded | `recover` of either strategy: the staging is removed, nothing was written |
| `publishing` / `published` / `needs_attention` | `recovering_<s>` | the lock is ours; no strategy was started yet, or the same strategy `<s>` is given (a different one is refused once a file was handled) |
| `recovering_<s>` | `recovering_<s>` (+1 file) | the file is re-read right before acting; the backup (rollback) or the staged copy (forward) matches the journal; the path is safe |
| `recovering_<s>` | `needs_attention` | at least one file was left untouched: a conflict, a tampered or missing copy, an unsafe path |
| `recovering_<s>` | `completed` | every file handled; a rollback also removes the directories the publication created when they are empty |
| `needs_attention` | `recovering_<s>` | the operator resolved the files (restored the expected or the new content) and runs the same strategy again |

## Decisions

1. **A project with an ambiguous tree is not written again.** `prepare` refuses while any other operation is pending
   or unreadable (`an interrupted operation exists`): adoption, generation and evolution all inherit it; `plan`
   already blocked (`no_pending_operation`, `no_unreadable_operation`).
2. **Every file is re-checked right before its own rename.** The precondition check before the first rename left a
   window as long as the publication of the earlier files; a file changed inside it was overwritten. Now the digest is
   compared again immediately before the rename and the staged copy is compared with the journal; a mismatch records
   the file as refused (`conflicts[path]` starting with `changed before it was written`), saves the journal and stops.
   A rollback then restores what landed and skips the refused file (it was never ours); a forward recovery reports it
   as a conflict. The window between that check and the rename is the rename itself.
3. **Guards.** A plan may name files it read but does not write (`guards: {path: digest}`, journal 1.1 optional key):
   the files an adopted plan resolved a dependency, a delegate or a response model in, the adopted and generated
   module files it did not rewrite, the Alembic ini, env and head revision of a persistence plan. Their digests must
   hold at `prepare` and at `publish`; a change is `concurrent change: <path>, which the plan read, changed`.
4. **Recovery trusts nothing it did not verify.** Every path goes through `safe_join` and the symlink check (a
   journal with `../x`, a path inside the operations directory or a symlink planted after the plan is `unsafe`,
   never touched); the staged copy and the backup are compared with the journal's digests before they are used
   (tampered or missing copies are conflicts); every file is re-read right before the action, not once at the start;
   the strategy a recovery started with is persisted (`recovery_strategy`) and a different one is refused while any
   file was handled, through `needs_attention` too.
5. **The journal is validated, not trusted.** Schema, identity (the `op_id` must be the directory's), unique and
   safe paths, 64-hex digests, a known state, `published` and `recovered` naming only files of the operation,
   well-formed steps, guards and `created_dirs`: a journal that fails is reported by `unreadable_operations` with the
   reason and blocks new plans and writes; it is never acted upon and never deleted. Stray files and symlinks inside
   the operations directory are reported too.
6. **Created directories go with a rollback.** The publication records the directories it had to create
   (`created_dirs`); a rollback removes them when they are empty, deepest first, so a rolled-back module leaves no
   empty package behind (an empty package directory is importable Python).
7. **`completed` before the cleanup.** The journal says `completed` before the staging is removed; a reader that
   finds such a journal finishes the cleanup instead of listing an interrupted operation; a fresh target rolled back
   keeps nothing of ours (`.fastapi-architect` goes when empty).
8. **The lock record is complete from its first visible moment.** The record is written to a temporary sibling and
   hard-linked into place (the link fails when the lock exists; `O_EXCL` plus an immediate write where links are not
   available): no reader sees an empty record of a live holder and calls it unreadable.
9. **Statuses say what is known.** `held` (alive on this host), `stale` (gone on this host), `unknown` (another
   host, or a pid this host cannot judge), `unreadable`, `invalid` (symlink or directory). **A lock of another host
   is never broken by age**: its age is reported, `ops unlock` refuses it, and only `ops unlock --other-host NAME`
   (the operator naming that host after confirming it) removes it; `break_stale` never touches it either. An
   unreadable record is removed on request only; an invalid lock by hand only. Inconsistent metadata (a pid that is
   not an integer, a missing host) is `unreadable`, never free. Waits stay bounded (`wait_s`); a held or unknown
   lock is waited for, the others fail at once.
10. **Leftover temporaries are named, never deleted.** `residual_temporaries` lists `*.fa-tmp` files of the project
    and the operations directory; `ops list` prints them and exits 1; the file next to each one is whole (the writer
    renames or leaves the previous content).
11. **The report keeps three facts apart.** `applied.scope` is the files; `applied.database` says whether a migration
    is required and that this tool did not apply it; `applied.deployment` says a written project and an applied
    migration are not a running application. An interrupted write claims nothing about the database. A failing
    revision is PostgreSQL's transaction: `alembic_version` and the schema stay where they were (scenario C shows
    it); the destructive contract re-checks its preconditions inside the revision, so a retry after a failure
    re-evaluates them instead of assuming anything (increment 2).
12. **Generation goes through the journal.** `run_design` writes a project through `write_recoverable` (`kind:
    generate`) under the project lock: an interrupted generation is recoverable, a rolled-back fresh target is
    empty, a locked target is a `GenerationError`.

## Defects found and fixed by this increment

- A file changed during the publication (after the single precondition check, before its own rename) was overwritten
  silently.
- A recovery resumed with the other strategy mixed restored and published files; `needs_attention` forgot the
  strategy.
- `recover` and `classify` joined journal paths without `safe_join`: a tampered journal could write outside the
  project; a symlink planted after the plan was followed.
- Tampered or missing staging copies and backups were used as if they were the journal's.
- A malformed journal that still had a schema and an `op_id` (duplicate paths, malformed digests, a `published`
  entry naming a stranger, an unknown state) was loaded and acted upon or crashed the reader.
- An adoption, a generation or a second evolution wrote a project whose journal had a pending or unreadable
  operation.
- A lock of another host was reported `stale` after six hours and `ops unlock` broke it; a pid that could not be
  judged counted as held without saying so; a non-integer pid crashed `inspect`; a lock written in two steps could be
  read empty.
- A rollback left the empty directories a module creation had made; a crash during the cleanup was reported as an
  interrupted publication; a fresh generation rolled back left `.fastapi-architect` behind.
- Generation wrote with a two-phase writer without journal or lock.

## Limits, stated

- **Hardware and filesystems.** Durability stops where `fsync` stops: the drive's own cache on macOS, directory
  entries on Windows, filesystems that refuse to sync directories (reported, not fatal). A filesystem that lies about
  `fsync` defeats the journal's durability, not its consistency on the next read.
- **Locks between hosts.** Nothing here can tell whether a process on another host is alive: such a lock is
  `unknown` until an operator says otherwise. Two hosts writing the same tree through a network filesystem are
  outside the model.
- **External changes without the lock.** A writer that ignores the journal and the lock is kept apart by content
  only: every digest is re-checked before each rename, and the rename itself is the remaining window.
- **Processes that die abruptly.** A kill between a rename and the journal save leaves a landed file the journal does
  not list; classification by digest finds it. A kill while the lock is held leaves a stale lock, removed on request.
- **PostgreSQL errors.** The journal says nothing about the database; a failed revision is rolled back by PostgreSQL's
  transactional DDL (`CREATE INDEX CONCURRENTLY` revisions excepted, by design, and verified by the phased runner).
- **Partial deployments.** A written project, an applied migration and a running application are three facts; the
  report names them apart and infers none from another. No coordinator exists across Git, the filesystem and the
  database, and none is promised.

## Consequences

- Journal 1.1 gains the optional keys `guards`, `created_dirs` and `recovery_strategy`, and the state `completed`;
  older journals validate as before. Operations returned by `write_recoverable` report `completed` once their
  cleanup ran (they reported `published` before).
- `FaultInjector` points `before_prepare`, `after_stage:<n>`, `after_journal`, `during_cleanup`, `before_recover`,
  `during_classify` join the existing ones.
- `locking.STATUSES`, `inspect` fields `older_than_stale_after`, `acquire(break_other_host=)`,
  `unlock_stale(other_host=)`, `evolve_fastapi.py ops unlock --other-host HOST`; `ops list|show` print
  `residual_temporaries`; `recover` exits 2 with the reason when the strategy would change.
- `generate(..., writer=)`; the pipeline passes the journaled writer.
- Tests: `tests/test_recovery_v22.py` (the fault matrix, foreign changes, guards, journal and lock integrity,
  generation, scenarios A, B, D, the command line), `tests/test_concurrency_v22.py` (ten cases with real processes),
  `tests/test_recovery_v22_postgresql.py` (scenarios C and D on a throwaway cluster, opt-in).
