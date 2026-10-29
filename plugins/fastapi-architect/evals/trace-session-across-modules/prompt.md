---
name: trace-session-across-modules
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

In our FastAPI service the route declares `db=Depends(get_db)` with no annotation, passes `db` to
`service.create(session, payload)`, which passes it to `OrderRepo().save(session, order)`. The repository calls
`session.commit()` without await and nothing fails loudly in tests. How would you find every place where this
untyped session is misused across modules, and what should change so the mistake cannot recur?
