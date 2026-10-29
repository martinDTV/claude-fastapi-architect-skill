---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It states that request context does not exist in the worker, so it must be captured explicitly in the job
   payload (tenant id, acting principal/actor, correlation id) when the job is enqueued, and recreated in the
   worker: a new session per job and the tenant setting applied inside that job's transaction (SET LOCAL /
   set_config(..., true)); it warns against a session-level SET on a pooled worker connection.
2. It distinguishes what is verifiable statically (the setting is applied per transaction, the job schema
   carries tenant_id) from what is not (that every job is enqueued with the right tenant, that authorization
   was checked before enqueueing), and treats the auditor's message as a question to answer with tests.
3. It proposes negative tests: a job for tenant A must not touch B's rows even if the handler is buggy; a job
   without tenant context must fail closed.
FAIL if it suggests the worker bypass RLS (owner/BYPASSRLS role) for convenience, or if it claims static
analysis can prove the worker is safe.
