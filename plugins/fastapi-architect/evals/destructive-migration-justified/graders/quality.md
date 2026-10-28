---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It does not block the migration on the finding alone: a drop is the contract step of expand-contract, and the finding is a review item whose context (no reader/writer, backfill done, old release retired) is exactly what justifies it.
2. It lists the checks that turn the justification into evidence before merging: grep/search that nothing references the column (including raw SQL, views, reports, ORM models), the previous release is really not running, a backup or archive of the column data exists or is explicitly unnecessary, downgrade is documented as irreversible or restores the column, and the migration is rehearsed on a copy.
3. It mentions deployment-order safety (the code that stops using the column must be deployed before the drop) and keeps the tone proportional: approve with evidence, not a rubber stamp.
FAIL if it says the migration must never drop columns, if it approves without any verification steps, or if it treats the high severity as a blocker by itself.
