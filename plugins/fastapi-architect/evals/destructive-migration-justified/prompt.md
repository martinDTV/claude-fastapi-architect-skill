---
name: destructive-migration-justified
tags: [v1.4, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

The auditor reported MIG_DROP_COLUMN (high) on our next Alembic migration, which drops `orders.legacy_status`. We finished the expand phase two releases ago: the column is no longer read or written, the replacement column is backfilled and NOT NULL, and the previous release is no longer deployed anywhere. Should we block the migration?
