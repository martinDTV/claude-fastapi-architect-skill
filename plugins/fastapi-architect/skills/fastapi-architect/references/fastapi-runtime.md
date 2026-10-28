# FastAPI runtime and ASGI behavior

## Trigger
Lifespan, dependency injection, workers, middleware, background tasks, SSE, WebSockets, streaming, OpenAPI or runtime crashes.

## Request lifecycle checklist
- Know which part runs on event loop vs threadpool. `async def` + blocking library can starve the worker; `def` endpoints run in a thread pool, not an isolation boundary for heavy CPU work.
- Initialize shared clients, DB engines and external connections in lifespan and close them on shutdown. Never create an `AsyncClient` for each request or an AsyncSession as a global singleton.
- Inject request-scoped dependencies with `Annotated`; test overrides reset in fixtures. Use app factories if multiple test instances are needed.
- Resolve dependencies per request and know the cache behavior of `Depends(use_cache=...)` and yield-based cleanup. Streaming must not use disposed request resources.
- Use pure ASGI middleware when request-body streaming, `contextvars`, and response streaming matter; account for ordering and exception handling.

## Timeouts and cancellation
- State deadlines at every boundary (ingress, application, database, remote client, queue). Budget backwards from user deadline.
- `asyncio.CancelledError` is control flow: do not swallow it or convert it into a success response. Ensure rollback and resource cleanup in `finally`/context managers.
- Client disconnect does not guarantee all long-running work has stopped. For durable operations, enqueue persistent jobs with an operation identifier.
- Reuse an application-lifespan httpx.AsyncClient with connection limits and per-phase timeouts. Retry only operations known safe or made idempotent.

## BackgroundTasks decision
Use FastAPI BackgroundTasks only for small best-effort operations whose loss on worker restart is acceptable. For contractual delivery, emails, exports, payment processing, polling or retries, use a durable queue, recorded state transitions, visibility timeout, and a worker.

## SSE / WebSockets / streaming
- Pick SSE for mostly server-to-client streams, WebSockets for bidirectional protocols, polling for environments with incompatible intermediaries.
- Plan authorization on connect and at meaningful renewal intervals, disconnect behavior, heartbeat, proxy buffering and idle timeouts.
- SSE needs event IDs if resume semantics are promised, a bounded replay log, and a clear Last-Event-ID retention contract.
- Streaming generators must handle cancellation, avoid leaking unbounded memory, release resources, and not emit secrets on exceptions.
- Gateway and cloud ingress capabilities can veto an otherwise attractive transport.

## OpenAPI and contracts
- Explicit response models and stable operation IDs. Review schema drift in CI. API routers should not return accidental ORM properties.
- Use dependency injection to enforce authentication, but resource-level authorization remains in service/domain policy.
- Ensure HTTP exceptions/errors map consistently to Problem Details, including validation, upstream timeouts, and rate limits.

## Validation matrix
Unit tests: dependency overrides and cancellation cleanup. Integration: lifespan resources, transaction rollback, streaming close, large payload limits. End-to-end: reverse proxy path prefixes, CORS, cookies, client disconnect and idle timeout. Performance: event-loop lag, queue wait, DB pool saturation and p95/p99 under concurrent load.
