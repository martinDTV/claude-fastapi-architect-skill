---
name: negative-django-project
tags: [v1.4, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

Our Django REST Framework project has fat views and a 2,000-line models.py. How should we split it into apps, and
where should business logic live? We are staying on Django.
