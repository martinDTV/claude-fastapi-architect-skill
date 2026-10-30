---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the mechanism: calling an `async def` returns a coroutine object that does nothing until
   awaited or scheduled; Python emits only a `RuntimeWarning: coroutine ... was never awaited` at garbage
   collection, not an exception, so the row is silently never written; the fix is `await`.
2. It explains how a static tool follows `self.record`: the method is resolved on the class (the receiver
   is `self`), the engine knows the callee is `async def`, and the call is neither awaited nor handed to
   `gather`/`create_task`/`TaskGroup` nor returned to a caller that awaits it; the finding carries the
   call path as evidence.
3. It gives prevention: run tests with `-W error::RuntimeWarning` (or `PYTHONWARNINGS`), a linter/type
   checker that flags unused coroutines (e.g. ruff's rule for unawaited coroutines or mypy's
   `unused-awaitable`), and tests that assert the side effect, plus the auditor in CI.
FAIL if it agrees that Python would raise, if it suggests `asyncio.run` inside the method, or if it
omits the warning-as-error mechanism.
