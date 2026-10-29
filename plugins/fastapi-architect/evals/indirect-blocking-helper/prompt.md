---
name: indirect-blocking-helper
tags: [v1.5, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

An async FastAPI endpoint `GET /report` calls `build_report(db)`, a plain `def` helper in another module.
`build_report` calls `fetch_rates()`, which uses `requests.get` with a 10 s timeout. Latency on unrelated
endpoints spikes whenever reports are requested. Explain what is happening and how to fix it properly.
