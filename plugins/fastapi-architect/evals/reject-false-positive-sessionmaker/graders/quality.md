---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It states clearly that the reviewer is wrong for this code: a module-level async_sessionmaker (or sessionmaker) is a factory, not a session, and creating one session per request through the yield dependency is the recommended pattern.
2. It explains the distinction with the case that WOULD be a defect: a module-level Session/AsyncSession instance (for example `session = SessionLocal()` or `AsyncSession(engine)` at import time) shared across requests and tasks, and says why that is unsafe (not task-safe, shared identity map and connection).
3. It does not recommend removing the global factory or wrapping it in locks, and it does not invent problems (such as claiming the engine must be created per request).
FAIL if it agrees with the reviewer, recommends locks or per-request engines, or fails to distinguish factory from instance.
