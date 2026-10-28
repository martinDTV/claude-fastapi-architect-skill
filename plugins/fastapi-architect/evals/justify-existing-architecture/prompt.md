---
name: justify-existing-architecture
tags: [v1.4, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our FastAPI backend is organised as src/shop/domain (pure dataclasses and Protocol ports), src/shop/application (use cases), src/shop/adapters (SQLAlchemy and HTTP implementations) and src/shop/api (routers). We have import-linter contracts that keep the domain pure and enforce the layering. A new tech lead wants to reorganise everything into modules/<capability>/router.py, service.py, repository.py because "that is the recommended FastAPI structure". Should we?
