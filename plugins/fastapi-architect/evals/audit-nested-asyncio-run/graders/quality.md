---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the cause: asyncio.run (or loop.run_until_complete) being invoked from code that already executes inside the running event loop (an async route, dependency, or a helper called from one), and it proposes a concrete way to locate it (static search/grep for asyncio.run and run_until_complete, or an AST-based audit rule).
2. The primary fix is to await the coroutine (or restructure so the sync helper is not called from async code); it does not present nest_asyncio or a new thread with its own loop as the default fix (mentioning them as last-resort workarounds with caveats is acceptable).
3. It proposes a regression test that exercises the failing code path under a running event loop (for example an async test that drives the endpoint through httpx.AsyncClient with ASGITransport, or a test that calls the helper from a coroutine), and it does not claim the cause is confirmed without having run such a check.
FAIL if nest_asyncio is the primary recommendation, if no reproduction is proposed, or if the explanation of the cause is wrong.
