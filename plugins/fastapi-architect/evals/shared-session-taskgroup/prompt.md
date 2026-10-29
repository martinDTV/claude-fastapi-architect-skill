---
name: shared-session-taskgroup
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

To speed up a FastAPI endpoint we load five aggregates in parallel with `asyncio.TaskGroup`, passing the
request's AsyncSession to each coroutine. It mostly works but sometimes fails with "This session is
provisioning a new connection; concurrent operations are not permitted". Is this a bug in SQLAlchemy?
