---
name: lost-response-double-charge
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

A customer was charged twice. Logs show our FastAPI endpoint called the payment provider, the call timed out
after 30 s, our client retried, and both attempts succeeded on the provider side. Explain the failure and
design the fix end to end (API, database, provider call).
