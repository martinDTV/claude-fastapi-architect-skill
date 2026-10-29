---
name: negative-nodejs-prisma-transactions
tags: [v1.5, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

In our Node.js service with Prisma, two concurrent requests both read a balance and write it back, and we
lose an update. How do I do an atomic increment with Prisma?
