---
name: resource-authorization-question
tags: [v1.7, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

`POST /accounts/{account_id}/close` depends on `get_context` (authenticated) and calls
`accounts.close(session, account_id)`. The auditor reports `SEC_RESOURCE_AUTHZ_UNVERIFIED` with
`evidence: unknown`, while `POST /accounts/{account_id}/freeze`, which calls
`accounts.freeze(session, ctx, account_id)`, gets nothing. Our service does check ownership inside
`close()` through a module-level `current_actor` ContextVar. Is the finding wrong, and should we restructure?
