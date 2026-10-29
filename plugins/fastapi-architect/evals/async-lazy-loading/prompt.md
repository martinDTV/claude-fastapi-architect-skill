---
name: async-lazy-loading
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our FastAPI app uses AsyncSession. An endpoint loads an Order with `await session.get(Order, id)` and later
does `for line in order.lines: total += line.amount`. In production it raises
`sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called`. Why, and what is the right fix?
