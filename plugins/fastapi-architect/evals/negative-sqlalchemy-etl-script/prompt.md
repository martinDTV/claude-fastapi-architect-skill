---
name: negative-sqlalchemy-etl-script
tags: [v1.5, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I have a standalone Python ETL script (no web framework) that uses SQLAlchemy 2.0 to copy rows between two
PostgreSQL databases every night. It opens one Session for the whole run. Should I batch commits, and how big
should the batches be?
