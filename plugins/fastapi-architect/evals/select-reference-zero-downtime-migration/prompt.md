---
name: select-reference-zero-downtime-migration
tags: [v1.4, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We need to change `orders.amount` from INTEGER to NUMERIC(12,2) and add a NOT NULL `currency` column in a PostgreSQL table with 80 million rows, without downtime, using Alembic. Lay out the migration plan step by step.
