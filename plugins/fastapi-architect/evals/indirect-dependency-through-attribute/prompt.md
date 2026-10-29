---
name: indirect-dependency-through-attribute
tags: [v1.6, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our `ReportService.__init__` receives `session: AsyncSession` and stores it as `self._session`. Its method
`async def summary(self)` calls `self._session.execute(select(...))` without `await` and returns the
result. Nobody noticed because `summary` is only called from a route three modules away through a
`get_report_service` dependency. The auditor flags `SA_ASYNC_SESSION_NOT_AWAITED` on the method. Is the
auditor right, how did it know the type of `self._session`, and what should we fix first?
