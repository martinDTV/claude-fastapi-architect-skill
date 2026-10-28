---
name: audit-nested-asyncio-run
tags: [v1.3, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Production logs of our FastAPI service show "RuntimeError: asyncio.run() cannot be called from a running event
loop" on one endpoint, intermittently. How do I find the cause and fix it properly? You cannot run anything here.
