---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It uses expand-contract: add new nullable columns (amount_v2 NUMERIC, currency), dual-write or backfill in batches outside the DDL transaction, switch readers, then SET NOT NULL / drop old column in later releases; it does not propose an in-place ALTER TYPE on 80M rows as the plan.
2. It handles the NOT NULL step safely (backfill first; NOT VALID check constraint then VALIDATE, or SET NOT NULL after the backfill; constant server_default where appropriate) and mentions lock_timeout and index creation CONCURRENTLY where indexes are involved.
3. It states how the plan is rehearsed and rolled back or rolled forward: run on an empty database and on a production-sized copy, previous release against the new schema, documented downgrade or explicit roll-forward.
FAIL if it proposes a single-step ALTER TYPE + ADD COLUMN NOT NULL, omits batching, or gives no rehearsal/rollback strategy.
