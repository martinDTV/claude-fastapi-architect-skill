---
name: tenant-propagation-worker
tags: [v1.5, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our multi-tenant FastAPI app uses PostgreSQL RLS with a transaction-local tenant setting. We are adding a
worker that processes jobs from a queue and writes to tenant tables. The auditor says it cannot verify the
worker's tenant context. How should tenant and principal context travel into the worker, and how do we prove
it?
