---
name: non-idempotent-retry
tags: [v1.5, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our payment call is wrapped in `@tenacity.retry(stop=stop_after_attempt(3))` and does
`await client.post("/charges", json={...})`. The static auditor says RETRY_NON_IDEMPOTENT_WRITE. The team
says retries are necessary because the provider times out. Who is right and what do we do?
