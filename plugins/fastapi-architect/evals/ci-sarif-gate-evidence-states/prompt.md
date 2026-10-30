---
name: ci-sarif-gate-evidence-states
tags: [v1.7, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

We want the FastAPI auditor in CI. It can emit SARIF and every finding has an `evidence_state`
(static_confirmed, inferred, insufficient, partial, suppressed). The team is split: some want to fail the
build on any finding, others say the questions (`insufficient`) will block every PR. Design the gate.
