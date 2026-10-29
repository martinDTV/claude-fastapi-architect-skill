---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains the actual risk of the shared global session (identity map and transaction state shared across
   requests and threads, failed-transaction state leaking) without exaggerating: low traffic hides it, it does
   not make it safe.
2. The plan is incremental: introduce the factory (sessionmaker/async_sessionmaker) and a per-request
   dependency alongside the global; migrate routes one module at a time (or by import), keep the global as a
   deprecated shim until the last caller moves, then delete it; characterisation tests first; the static
   auditor or import-linter to stop new uses of the global.
3. It does not propose a big-bang rewrite, does not require moving to async as a precondition, and states
   how each step is verified and rolled back.
FAIL if it proposes replacing everything at once, if it says the current code is fine, or if there are no
verification steps.
