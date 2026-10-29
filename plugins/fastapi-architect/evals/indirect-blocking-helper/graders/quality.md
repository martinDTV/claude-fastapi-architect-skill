---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the mechanism: the sync helper runs on the event loop thread because the endpoint is async,
   so the blocking requests.get stalls every other request on that worker; the blocking call is two calls
   away from the route (route -> build_report -> fetch_rates), which is why a quick look at the endpoint
   misses it.
2. It offers the right fixes with trade-offs: make the chain async (httpx.AsyncClient), or offload the sync
   helper with anyio.to_thread.run_sync / asyncio.to_thread, or declare the endpoint with plain def so FastAPI
   runs it in the threadpool; it does not suggest increasing workers as the primary fix.
3. It proposes how to confirm and prevent regressions: a concurrent-latency or event-loop-lag test, and a
   static check that follows call paths (the skill's ASYNC_BLOCKING_CALL_INDIRECT rule or an equivalent).
FAIL if it only inspects the endpoint body, blames the database, or proposes more workers as the solution.
