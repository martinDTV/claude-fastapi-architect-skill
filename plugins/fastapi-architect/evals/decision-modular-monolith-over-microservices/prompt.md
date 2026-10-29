---
name: decision-modular-monolith-over-microservices
tags: [v1.6, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We run a FastAPI monolith with four business modules (patients, scheduling, billing, notifications),
one PostgreSQL, a team of five, about 200 requests per second at peak. Deploys are weekly and sometimes
scary because billing and scheduling share tables. Leadership wants to "move to microservices" this
quarter. What would you do?
