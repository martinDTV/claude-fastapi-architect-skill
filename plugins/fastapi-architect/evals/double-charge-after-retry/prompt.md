---
name: double-charge-after-retry
tags: [v1.6, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Production incident: a customer was charged twice. Logs show the first `POST /payments` request from our
mobile app timed out client-side after 30 s and the app retried; both requests reached the server and both
inserted a payment and called the provider. Our handler is `async def create_payment(payload, session:
SessionDep)` with no retry logic server-side. Where exactly is the defect, what proves it, and what is the
minimal correct fix?
