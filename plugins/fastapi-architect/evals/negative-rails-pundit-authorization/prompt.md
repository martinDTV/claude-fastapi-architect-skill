---
name: negative-rails-pundit-authorization
tags: [v1.7, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

In our Rails app we use Pundit policies, but a controller action skipped `authorize` and a user could see
another account's invoices. How do we make sure every action calls `authorize` or `policy_scope`?
