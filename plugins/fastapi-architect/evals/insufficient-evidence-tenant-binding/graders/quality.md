---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It distinguishes authentication from authorization: knowing who the caller is does not establish that
   the caller may act on the tenant named in the URL; the finding is about the missing visible binding
   between the requested tenant and the authenticated principal.
2. It explains `evidence: unknown` honestly: the tool found neither a membership check nor the principal
   flowing into the call that receives the tenant id, so it cannot decide; it is a question, not a
   confirmed vulnerability, and the lead may be right if the check lives in middleware, row-level security
   keyed by the principal, or inside the service.
3. It proposes how to settle it: a cross-tenant negative test (member of tenant A requests tenant B's
   summary and must get 403/404 and no data), and if the check is missing, a membership dependency
   (`require_member(tenant_id, user)`) or passing the principal to the service so the binding is explicit
   and visible.
FAIL if it declares a confirmed vulnerability without the test, if it dismisses the finding because
authentication exists, or if it suggests suppressing it without evidence.
