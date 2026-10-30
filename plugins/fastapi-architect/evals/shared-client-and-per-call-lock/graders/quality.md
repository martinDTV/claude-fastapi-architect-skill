---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. On the client: it explains that a per-call `AsyncClient` loses connection reuse and leaks sockets or
   pooled connections until garbage collection (which in asyncio is late and noisy), and gives the right
   shape: one client for the application created in the lifespan and stored on `app.state` or injected,
   or `async with httpx.AsyncClient() as client:` when a short-lived client is really needed.
2. On the lock: it explains that a lock created inside the function is a new object per call, so
   concurrent calls never contend and nothing is protected; a lock must be shared (module or object
   attribute) to mean anything, and it adds that an in-process lock does not protect a database invariant
   across workers anyway: the transfer needs a database-level guarantee (atomic UPDATE, `SELECT ... FOR
   UPDATE`, or an optimistic version).
3. It keeps both as real findings with their mechanism and names a check for each (socket count under
   load; two concurrent calls both inside the critical section).
FAIL if it accepts the garbage-collector argument, if it says the per-call lock protects the transfer,
or if it recommends the lock as the fix for the money transfer.
