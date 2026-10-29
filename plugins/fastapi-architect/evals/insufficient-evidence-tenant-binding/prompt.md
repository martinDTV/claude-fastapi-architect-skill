---
name: insufficient-evidence-tenant-binding
tags: [v1.6, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

The auditor reports `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` with evidence `unknown` on
`GET /tenants/{tenant_id}/summary`. The route depends on `get_context`, which depends on `get_current_user`,
and it passes `tenant_id` straight to `reports.summary(session, tenant_id)`. Our tech lead says "we have
authentication, the finding is wrong". Is it a vulnerability? What does `evidence: unknown` mean here and
how do we settle it?
