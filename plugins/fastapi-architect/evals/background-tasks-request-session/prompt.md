---
name: background-tasks-request-session
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

After creating an order, our FastAPI endpoint does `background_tasks.add_task(send_invoice, db, order.id)`
where `db` is the request's AsyncSession from `Depends(get_session)`. Invoices are sent, but we see
"connection is closed" errors and the pool slowly drains. What is wrong?
