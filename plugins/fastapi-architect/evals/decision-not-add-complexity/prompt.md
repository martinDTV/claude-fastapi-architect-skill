---
name: decision-not-add-complexity
tags: [v1.5, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We have an internal FastAPI tool with 6 endpoints, one developer, SQLite in production, no concurrency to
speak of. A consultant recommends adding a repository layer, a unit-of-work abstraction, CQRS and an event
bus "to be ready for scale". Should we?
