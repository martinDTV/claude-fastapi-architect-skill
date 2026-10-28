---
name: security-not-verifiable-statically
tags: [v1.4, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

The auditor flagged SEC_TENANT_ID_FROM_REQUEST on `GET /tenants/{tenant_id}/invoices` in our FastAPI app. The handler has `user: CurrentUser` injected, but the tool says it cannot verify membership statically. Our team wants a yes/no: is the endpoint safe?
