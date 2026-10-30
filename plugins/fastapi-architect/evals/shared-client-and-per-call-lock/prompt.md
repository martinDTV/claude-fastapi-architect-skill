---
name: shared-client-and-per-call-lock
tags: [v1.7, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Two findings on one service: `REL_HTTP_CLIENT_PER_CALL` because `rates()` does
`client = httpx.AsyncClient(); return (await client.get(url)).json()`, and `ASYNC_LOCK_PER_CALL` because
`transfer()` does `lock = asyncio.Lock(); async with lock: ...`. The author says the client is "cleaned up
by the garbage collector" and the lock "protects the transfer". Are these real, and what is the right
shape for each?
