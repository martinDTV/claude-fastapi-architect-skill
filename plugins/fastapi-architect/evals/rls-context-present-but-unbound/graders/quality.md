---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the real risk precisely: RLS enforces whatever tenant the context names; when the context
   comes from the URL and nothing ties that tenant to the authenticated principal, any authenticated user
   can set the context to another tenant and read it. The check is present (RLS works) but not bound to
   the caller; correct RLS configuration does not help because the policy is given the attacker's value.
2. It gives the fix: bind membership before setting the context (a dependency that receives `tenant_id`
   and the principal and checks membership, returning 404 for non-members), or derive the tenant from
   the principal instead of the URL, and only then `set_config`; it explains that `evidence: verified`
   means the mechanism is visible in the code, while the impact still needs the cross-tenant test.
3. It proposes the proof: as a member of tenant A, request tenant B's export and expect 404/403 with no
   rows; and it keeps the positive path (A's own export works).
FAIL if it says correct RLS configuration makes this safe, if it suggests removing RLS, or if it omits
the binding step.
