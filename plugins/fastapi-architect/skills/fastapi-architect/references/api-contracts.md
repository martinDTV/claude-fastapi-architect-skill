# HTTP API contracts: safety, compatibility, and ergonomics

## Operation design
Specify resource identity, authorization, state transitions, permitted errors, and consistency semantics before implementing a route. Use OpenAPI stable operation IDs and schema names. Model RFC 9457 problem details with safe codes and request IDs; never expose internals, secrets, SQL, or whether an unauthorized resource exists.

## Pagination/filter/sort
- Choose cursor pagination for large, frequently changing feeds; encode ordering values plus unique tie-breaker and sign or otherwise protect cursors. Specify deterministic sort, cursor expiry and filter binding.
- Offset pagination is valid for bounded or simple datasets; deterministic ordering with a unique key is still mandatory. Enforce max limit, avoid costly unrestricted counts.
- Make sort a server-side allowlist mapping symbolic names to known SQL expressions; never interpolate client identifiers into SQL.
- Define search privacy: even HTTPS URLs may reach access logs, browser history and observability. Consider POST search for sensitive queries, otherwise sanitize logs and avoid reflecting search terms in URLs unnecessarily.

## Concurrency and idempotency
- Use `ETag`/`If-Match` for conditional modification where the resource has a stable version. Missing precondition may be 428; stale version normally 412. Compare-and-swap MUST be atomic in SQL; a read-then-write check is race-prone.
- Idempotency: define scope `(tenant, identity, endpoint, key)`, request fingerprint, durable reservation and result, TTL, concurrent requests, body mismatches, and the recovery path after an uncertain outcome. Preserve key across retries of the *same* operation; never reuse for a changed request.
- A 409 conveys an application conflict; do not promise retries will succeed. A failed HTTP response does not guarantee that an upstream write did not occur. Require explicit retry policy per operation.

## Compatibility
- Additive fields and optional parameters usually allow older clients; changing enums, authorization, status codes, or previously optional fields can break them. Test both consumer and provider versions.
- Prefer expand/contract migrations and deprecation windows for public APIs. Document pagination, time zones, concurrency guarantees, rate-limit headers, and security semantics.
- Expose required cross-origin headers through precise CORS configuration. CORS is not an authorization mechanism.

## Time and money
Store instants UTC, preserve IANA time zone for wall-clock rules, model ambiguous and nonexistent local times explicitly. Use Decimal / integer minor currency units, not float, for finance.

## Checklist
[ ] Proven tenant/resource authorization; [ ] stable ordering; [ ] bounded input/output; [ ] safe errors; [ ] idempotency/conditional semantics if relevant; [ ] cache headers; [ ] OpenAPI compatibility; [ ] cancellation/timeouts; [ ] test at least one concurrent or adversarial case.
