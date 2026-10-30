---
name: declarative-architecture-contracts
tags: [v1.7, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We have a modular monolith with `api/`, `services/` and `infra/` packages and three teams. Routers
sometimes commit directly and import infrastructure clients. The auditor supports declared contracts in
`.audit-fastapi.json`. Which contracts should we declare first, how do we introduce them without blocking
everyone, and why does it matter that the configuration is JSON rather than Python?
