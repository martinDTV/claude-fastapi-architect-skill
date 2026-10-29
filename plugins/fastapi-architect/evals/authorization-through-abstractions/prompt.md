---
name: authorization-through-abstractions
tags: [v1.6, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

Our FastAPI routes never call anything named `authorize`. Each handler takes `ctx: RequestContext =
Depends(get_context)`; `get_context` depends on `get_current_user` and builds a `Policy` object; services
call `ctx.policy.require("accounts:write", resource)`. A security scanner flagged all 40 routes as
"missing authorization". Is our design wrong, and how should a static tool reason about it?
