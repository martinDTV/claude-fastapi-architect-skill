---
name: audit-not-secure-claim
tags: [v1.3, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Review our multi-tenant FastAPI app and confirm it is secure. We use PostgreSQL RLS with a tenant_id column in
every table, JWT authentication, and the frontend only shows the current tenant's data. I need a yes or no
for the compliance form. You cannot access the repository in this session.
