---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the lost-response problem: a timeout does not mean the charge failed; the first attempt may
   have been applied and the retry charges again; so the retry itself is not wrong, retrying a non-idempotent
   write blindly is.
2. It proposes the fix pattern: an idempotency key generated once per business operation and sent on every
   attempt (provider-side dedup), persisting the key/intent before the call, and reconciliation for unknown
   outcomes (query the provider by key before retrying); it keeps the retry with bounded attempts and
   backoff, only on transient errors.
3. It describes how to prove it: a fault-injection test where the provider applies the write and then drops
   the response; assert exactly one charge.
FAIL if it says to remove retries, if it says the retry is fine as-is, or if it omits the idempotency key.
