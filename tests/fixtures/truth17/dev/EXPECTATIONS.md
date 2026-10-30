# truth17 / dev — expectations written before the v1.7 detectors

Marker legend: `# truth: RULE [evidence=verified|inferred|unknown]` (must be reported, optionally with that evidence
class), `# truth-absent: RULE` (must not be reported in the enclosing function), `# truth-edge: callee`,
`# truth-unresolved: STATUS`. Expectations are independent of the engine: they describe what a careful reviewer
would conclude from the code alone. This partition is used while implementing v1.7; the `acceptance` partition is not.

| Area | Case | Expected |
|---|---|---|
| Contracts | `/health`, `/webhooks/*` (declared public in `.audit-fastapi.json`), `/pricing` (`# audit-fastapi: public`) | no unauthenticated-route finding |
| Contracts | `/orders` with no dependency and no declaration | `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` |
| Contracts | three-level dependency chain, class-based role checker, `Security` scopes, router-level role dependency, permission factory | authenticated; no finding |
| Resource authorization | authenticated route passing only the resource id to a service that checks nothing | `SEC_RESOURCE_AUTHZ_UNVERIFIED` as a question (`evidence=unknown`) |
| Resource authorization | principal flows into the service, a policy call in the body, a permission dependency, or the id is never used | no finding |
| Privilege change | role assignment behind authentication only | `SEC_PRIVILEGE_CHANGE_UNVERIFIED` (`evidence=unknown`); silent behind `RoleChecker("admin")` or `@requires("admin")` |
| Tenancy | RLS context set from the requested tenant without membership or a comparison with the principal | `SEC_RLS_CONTEXT_FROM_REQUEST`; the generic tenant-binding question is not repeated for the same route |
| Tenancy | membership dependency, comparison with the principal, or context from the principal | no finding |
| Tenancy | requested tenant filters a query with authentication only | `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` (`evidence=unknown`) |
| Tenancy | background task receiving the tenant id and recreating session and context | no finding; request session handed to a task: `BG_REQUEST_SCOPED_RESOURCE` |
| Tenancy | `SET app.tenant_id` without LOCAL | `SEC_RLS_SET_NOT_LOCAL` |
| Resources | project coroutine called without `await` (assigned or returned from `async def`) | `ASYNC_COROUTINE_NOT_AWAITED`; silent when awaited, gathered, scheduled, or returned from a sync wrapper |
| Resources | `asyncio.Lock()` created inside the function that uses it | `ASYNC_LOCK_PER_CALL`; module-level lock silent |
| Resources | `httpx.AsyncClient()` built per request without `async with` or `aclose()` | `REL_HTTP_CLIENT_PER_CALL`; managed, closed or shared clients silent |
| Resources | one session under `gather` | `SA_SESSION_SHARED_CONCURRENTLY`; shared client and one session per task silent |
| Reliability | retried write without key, unbounded retry, requests without timeout, publish after commit, read-modify-write, external call inside transaction, retry loop around a write | reported |
| Reliability | key header, bounded retries, timeout tuple, outbox row, inbox dedup, atomic update, `FOR UPDATE`, reconciliation after timeout | silent |
| Consumers | event handler that writes with no redelivery guard | `REL_CONSUMER_WITHOUT_INBOX` as a question (`evidence=unknown`); silent with an inbox/processed insert under `ON CONFLICT` or when the handler does not write |
| Tests | test code using shared sessions and requests | shared session reported with lower confidence (test code is classified, never hidden); HTTP timeout silent (reliability checks target application code) |

Changes after the first write are recorded at the bottom of this file with the reason.

## Change log
- 2026-10-29, before implementing: the test-code row first said "silent" for a shared session in a test. That contradicted the policy in force since v1.3 (findings in test code are reported with `environment: test` and one confidence level less, never hidden). The marker on `tests/test_app.py` became `truth: SA_SESSION_SHARED_CONCURRENTLY`; the HTTP-timeout expectation stays silent because the reliability module does not run on test code.
- 2026-10-30, before implementing the consumer signal: `app/services/consumers.py` added with a positive, a guarded negative and a read-only negative for `REL_CONSUMER_WITHOUT_INBOX`, a rule the first write of this partition did not anticipate. No existing expectation changed.
