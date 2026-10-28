# PostgreSQL design and safe evolution

## Ownership and transaction boundaries
Separate transaction-scoped unit of work from query implementation. Whether `service` or `unit_of_work` commits must be chosen per project and documented; do not mix strategies silently. A repository may return a selected projection rather than always exposing ORM entities.

## Correctness
- Database constraints are the final concurrent guard: unique keys, composite tenant FKs, checks, NOT NULL, exclusion constraints where suitable.
- Avoid implicit lazy loading in async ORM. Inspect `EXPLAIN (ANALYZE, BUFFERS)` for expensive queries in a safe environment and sanitize captured queries.
- Missing indexes on tenant + lookup and foreign keys cause high latency; excessive indexes inflate writes. Select based on cardinality and observed queries.
- Enforce pagination, statement timeout and resource quotas; hot-path counts may need approximation.

## Expand/contract rollout
1. Add nullable/default-safe structure and nonbreaking API behavior.
2. Deploy code that handles old and new representations.
3. Backfill in resumable bounded batches with checkpoints.
4. Verify integrity and downstream consumers.
5. Flip reads/writes; remove old structure only after compatibility window.

Use concurrent index creation where supported and ensure migrations are not transactionally wrapped in incompatible ways. Test downgrade only where data-preserving and genuinely supported; don't promise reversible destructive changes.

## Disaster recovery
Backups require restore drills, encryption, retention, RPO/RTO, point-in-time recovery if critical, role/secret restoration, and tenant-access validation on restored data. A backup without a tested recovery procedure is not evidence of recoverability.

## Zero-downtime schema changes with Alembic and PostgreSQL (v1.4)

**Expand-contract.** Every incompatible change is three releases: expand (additive, both code versions work), migrate data (backfill in batches, dual-write if needed), contract (remove the old shape once no reader or writer remains). The auditor reports the contract operations (`MIG_DROP_TABLE`, `MIG_DROP_COLUMN`, `MIG_TYPE_CHANGE`) as changes that need review, not as errors: the review confirms the expand and backfill phases are done and the previous release no longer runs.

**Backfills.** Never inside the DDL transaction (`MIG_DDL_WITH_DML`): the schema locks are held for the whole update and a failure cannot be resumed. Backfill in batches (`UPDATE ... WHERE id BETWEEN ...` or keyset), from a separate migration or from the application, idempotently. Seeding a table created in the same migration is fine and is not reported.

**NOT NULL and defaults.** `ADD COLUMN ... NOT NULL` without a default fails on populated tables; with a constant default PostgreSQL 11+ stores it in the catalog without rewriting. Pattern: add nullable, backfill, `ALTER ... SET NOT NULL` (which scans the table under a lock; on large tables add a `NOT VALID` check constraint, validate it, then set NOT NULL). `MIG_NOT_NULL_WITHOUT_DEFAULT` points at both steps.

**Indexes.** `CREATE INDEX` blocks writes for its duration; `CREATE INDEX CONCURRENTLY` does not, but cannot run inside a transaction (`with op.get_context().autocommit_block():`), takes longer, and leaves an `INVALID` index on failure that must be dropped and retried. Indexes on a table created in the same migration are free (`MIG_INDEX_NOT_CONCURRENT` ignores them).

**Locks.** `ALTER TABLE` variants differ: adding a nullable column or a constant default is instant; changing a type usually rewrites; adding a foreign key validates the whole table unless `NOT VALID` then `VALIDATE CONSTRAINT`. Set `lock_timeout` in migrations so a stuck `ACCESS EXCLUSIVE` request fails fast instead of queueing every reader behind it.

**Rollback and roll-forward.** Write `downgrade()` when the change is reversible; when it is not (data destroyed, type narrowed), say so in the revision and plan roll-forward: fix forward with a new revision, restore from backup only as the last resort. `MIG_DOWNGRADE_NOOP` marks revisions that cannot be rolled back with Alembic.

**Verification.** Rehearse every migration on an empty database and on a copy with production-like volume: `alembic upgrade head`, run the previous application release against the new schema, run the new release, `alembic downgrade -1` where supported, and `alembic check` for drift. The auditor never executes migrations; its output is the checklist for this rehearsal.
