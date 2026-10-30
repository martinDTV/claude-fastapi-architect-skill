---
name: coroutine-never-awaited-silent-failure
tags: [v1.7, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our audit log has gaps. In `AuditLog.record_twice` we call `self.record(staff, "a")` and then
`await self.record(staff, "b")`; only "b" rows exist. The auditor reports `ASYNC_COROUTINE_NOT_AWAITED` on
the first call with a call path through `self.record`. A colleague says Python would have raised an
error if a coroutine were never run. What happened, how did the auditor know `record` is async through
`self`, and how do we prevent this class of bug?
