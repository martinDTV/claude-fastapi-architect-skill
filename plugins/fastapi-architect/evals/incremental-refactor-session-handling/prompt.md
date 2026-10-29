---
name: incremental-refactor-session-handling
tags: [v1.5, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

A legacy FastAPI app (about 40 routes) has `session = Session(engine)` at module level in db.py and every
route imports it. It works in production with low traffic. Management wants it fixed without a rewrite and
without downtime. Give me the plan.
