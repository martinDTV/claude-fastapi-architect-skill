---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It does not present "safe" or "unsafe" as a settled fact: it states that the endpoint's safety is not verifiable from the code alone (an opening such as "no, not on this evidence" or "not verifiable yet" is fine) and explains why: a visible authentication dependency proves who the caller is, not that tenant_id from the path is bound to that caller's memberships; that binding is a runtime property.
2. It describes the concrete negative test that settles it: authenticate as a member of tenant A and request tenant B's identifier through this route (single resource, list, search/aggregate variants); expect 403/404 and no rows; plus the positive path for A.
3. It names where the binding can legitimately live (a membership dependency such as require_member(tenant_id, user), router-level dependencies, RLS with a transaction-local tenant context) and recommends making the check explicit and tested rather than relying on the absence of a finding.
FAIL if it asserts "safe" or "unsafe" as a settled fact, if it treats CurrentUser as sufficient, or if it provides no cross-tenant test.
