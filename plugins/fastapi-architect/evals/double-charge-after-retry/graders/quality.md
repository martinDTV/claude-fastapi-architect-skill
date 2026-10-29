---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It locates the defect in the operation's lack of idempotency, not in the client's retry: a timeout is
   a lost response, the first request may have succeeded, and a non-idempotent write applied twice is the
   expected outcome; the server must make the retry safe because clients will always retry.
2. It gives the minimal correct fix: an idempotency key generated once per business operation by the
   client, stored server-side with a uniqueness guarantee (unique constraint on tenant/key) inside the same
   transaction as the payment, replaying the stored response on a repeat and resolving concurrent duplicates
   by the constraint (one wins, the other returns the winner's result); the provider call also carries the
   key where supported.
3. It states how to prove it: a test that sends the same request twice (and ten times concurrently) with
   one key and asserts exactly one payment and one provider call; and it mentions reconciliation for the
   case where the provider applied a charge whose response was lost.
FAIL if it blames the mobile app's retry as the thing to remove, if it proposes "check if a payment exists
then insert" without a constraint (race), or if it omits the test.
