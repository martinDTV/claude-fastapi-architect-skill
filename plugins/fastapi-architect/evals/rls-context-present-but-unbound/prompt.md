---
name: rls-context-present-but-unbound
tags: [v1.7, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We use PostgreSQL row-level security with a non-owner role and `set_config('app.tenant_id', ..., true)` per
transaction. Our `GET /tenants/{tenant_id}/export` route is authenticated (`Depends(get_context)`) and
sets the context from the `tenant_id` in the URL before querying. The auditor reports
`SEC_RLS_CONTEXT_FROM_REQUEST` with `evidence: verified`. RLS is configured correctly and tested. Why is
this flagged and what is the actual risk?
