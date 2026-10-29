---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains that the dependency-provided session is request-scoped: the dependency's teardown closes it
   when the response is sent, while the background task is still running on it, which produces closed
   connection errors and can leak pooled connections.
2. It gives the fix: pass identifiers, not sessions or ORM objects; create a fresh session inside the task
   from the async_sessionmaker (and re-establish any tenant/authorization context there); and it points out
   when BackgroundTasks is not durable enough (work that must survive a restart belongs in an outbox/worker).
3. It proposes a test: run the endpoint, let the task outlive the response, assert the task's work and the
   pool returning to baseline.
FAIL if it recommends keeping the session open longer, blames the pool size, or omits creating a session
inside the task.
