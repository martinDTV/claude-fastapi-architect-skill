---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the lost response: a timeout is an unknown outcome, not a failure; retrying a non-idempotent
   call duplicates the effect.
2. The design covers the three layers: the API accepts an idempotency key per payment attempt (or derives
   one from the order) and stores the attempt before calling out; the database records the intent and the
   outcome (unique key, state machine or outbox), so a replay returns the stored result; the provider call
   carries the same idempotency key so the provider deduplicates; unknown outcomes are reconciled by querying
   the provider before any retry.
3. It includes verification: a test that simulates the provider applying the charge and dropping the
   response, asserting one charge; and mentions bounded retries with backoff only for transient errors.
FAIL if the fix is just "do not retry", if the idempotency key is missing at the provider boundary, or if
there is no reconciliation for unknown outcomes.
