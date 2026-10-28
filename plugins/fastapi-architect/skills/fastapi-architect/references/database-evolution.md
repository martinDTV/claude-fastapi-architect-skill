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
