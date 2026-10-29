---
name: negative-django-celery-task-retries
tags: [v1.6, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

In our Django project a Celery task that sends invoices is configured with `autoretry_for=(Exception,)` and
no `max_retries`, and we saw a task retry for hours. How should I configure Celery retries and backoff?
