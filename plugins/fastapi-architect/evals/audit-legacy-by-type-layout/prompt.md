---
name: audit-legacy-by-type-layout
tags: [v1.3, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our FastAPI codebase is organised as routers/, models/, services/ and utils/ with about 70 endpoints, one team of
four developers, and no automated tests. Refactor it to the recommended architecture. Give me the plan; you
cannot read the repository in this session.
