---
name: apparently-insecure-but-correct-shared-client
tags: [v1.6, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

A reviewer wants us to stop sharing one `httpx.AsyncClient` across all requests and across
`asyncio.gather(client.get(a), client.get(b))` because "shared objects under concurrency are unsafe, like
the session". In the same service we also call `dispatcher.publish(session, event)` after `commit()`, where
`dispatcher` is our own in-process `EventDispatcher` that writes to an `events` table with the same session.
The reviewer calls that a dual write. Are these two findings real?
