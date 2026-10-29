---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It confirms the defect: `AsyncSession.execute` returns a coroutine, so without `await` the query never
   runs and the method returns an un-awaited coroutine (Python also warns "coroutine was never awaited");
   the fix is to add `await`.
2. It explains how a static tool can know the attribute's type: the annotation on the `__init__` parameter
   that is assigned to `self._session` (or the constructor's argument types where the service is built),
   i.e. the type flows from the constructor to the methods; and it notes that this is resolved from the
   source without running anything.
3. It recommends a regression test that exercises `summary` through the real route or the service with a
   real session, and suggests making the type explicit where it is not (annotations), so the attribute
   kind is `verified` rather than inferred.
FAIL if it says the auditor cannot know the type, if it suggests wrapping the call in `asyncio.run`, or if
it proposes replacing the session with a sync one.
