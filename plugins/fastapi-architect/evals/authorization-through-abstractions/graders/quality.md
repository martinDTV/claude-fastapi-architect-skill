---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It says the design is legitimate: authorization implemented through a composed dependency and a policy
   object is a recognised pattern; the absence of a function literally named `authorize` proves nothing,
   and a tool that flags every route for that reason is reporting the absence of a naming convention, not
   a defect.
2. It explains how a careful static tool should reason: follow the dependency chain (`get_context` ->
   `get_current_user`) to recognise authentication, look for where the policy check is called relative to
   the resource, and when it cannot see the check it should report "insufficient evidence" with low
   confidence rather than a missing authorization.
3. It still insists on proof: per-resource negative tests (a user without `accounts:write` gets 403 on
   every mutating route; cross-tenant resources are invisible), and a way to make the check visible and
   hard to forget (a dependency per protected route or a service-layer guard that every use case passes
   through), plus a test that fails when a new route skips it.
FAIL if it tells them to rename functions to satisfy the scanner, if it accepts the 40 findings as real,
or if it says tests are unnecessary because the abstraction exists.
