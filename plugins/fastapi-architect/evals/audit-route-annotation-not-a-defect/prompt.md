---
name: audit-route-annotation-not-a-defect
tags: [v1.3, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

A static auditor reported ROUTE_NO_RETURN_ANNOTATION on 40 of our FastAPI routes. Is our API broken? Should I
open a security incident? Most of those routes already declare response_model.
