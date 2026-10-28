---
name: audit-blocking-triage
tags: [v1.3, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

I inherited a FastAPI service (about 40 routes, PostgreSQL, one small team). Several `async def` routes call
`requests.get(...)` to a partner API and one calls `time.sleep(2)` as a "retry delay". The previous team says it
works fine in production. You cannot run anything in this session. Audit this: tell me what is wrong, how bad it
really is, how you would verify it, and what you would do first.
