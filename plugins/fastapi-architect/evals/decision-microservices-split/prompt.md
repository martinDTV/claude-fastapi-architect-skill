---
name: decision-microservices-split
tags: [v1.3, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our FastAPI app has about 60 endpoints and it is getting hard to navigate. One team of five developers,
one PostgreSQL database, deployed as a single container. Let's split it into microservices. Give me the
split: which services, and in what order. You cannot read the repository in this session.
