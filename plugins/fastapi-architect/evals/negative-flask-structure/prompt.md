---
name: negative-flask-structure
tags: [v1.3, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

Our Flask application has 25 routes in a single app.py with the SQLAlchemy models in the same file.
How should we organise the project? We are staying on Flask; no migration planned.
