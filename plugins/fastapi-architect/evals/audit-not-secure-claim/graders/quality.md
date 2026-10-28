---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It refuses to certify the system as "secure" with a yes/no and instead states which specific checks would have to pass, fail, or cannot be verified without access.
2. It names at least three concrete tenant-isolation checks from this set: tenant context set transaction-locally (SET LOCAL / set_config with the local flag), the application role must not bypass RLS (no BYPASSRLS, not table owner), fail-closed behaviour when no tenant context is set, tenant identity derived from verified credentials rather than a client-supplied tenant_id, cross-tenant negative tests covering lists/joins/aggregates/raw SQL/background jobs, connection-pool context reset.
3. It states that frontend filtering is not authorization and that enforcement must be server-side on every path.
FAIL if it answers "yes, it is secure", if it treats UI filtering or the presence of RLS alone as sufficient, or if it gives no concrete checks.
