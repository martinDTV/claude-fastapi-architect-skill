---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It proposes a concrete reproduction with two independent sessions (or two concurrent requests) against a real database: both load the same product, both decrement, both commit; and states the expected wrong outcome (stock decremented once, or negative stock / oversell) versus the correct one.
2. It presents at least two mechanisms with their trade-offs: an atomic conditional UPDATE (stock = stock - :qty WHERE stock >= :qty, checking rowcount, optionally with a CHECK constraint), SELECT ... FOR UPDATE inside one transaction, or optimistic concurrency with a version column / If-Match; and recommends one for this case with a reason.
3. It does not recommend locking everything by default, and it says the static finding is a signal to be reproduced, not proof.
FAIL if there is no concrete two-writer test, if it asserts the race is proven without a test, or if it prescribes SELECT FOR UPDATE universally without alternatives.
