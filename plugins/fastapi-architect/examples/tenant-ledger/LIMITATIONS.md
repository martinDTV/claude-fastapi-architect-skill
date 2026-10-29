# Known limitations

- **Authentication is a demo.** `X-Demo-User` names the caller; there is no credential, token or session. Nothing here should be read as guidance for authentication; the example is about authorization, isolation and persistence once a principal is known.
- **Tenant creation is open.** Any authenticated demo user can create a tenant and becomes its owner. A real system would gate this behind billing or an administrator.
- **Platform tables carry no RLS.** `tenants`, `users` and `memberships` are readable by the application role without tenant context because membership lookup is what establishes the context. Column-level exposure of those tables (for example a user listing) is not part of the example.
- **One application role, one schema.** Per-tenant roles or schemas, connection poolers in transaction mode and `SET ROLE` patterns are not covered. The transaction-local `set_config` approach shown here is correct for a single role behind SQLAlchemy's pool; with PgBouncer in transaction mode it still holds because the setting never outlives the transaction.
- **Idempotency keys never expire** and store the full response. Production systems bound retention and may store only a result reference.
- **Balance arithmetic uses integer minor units** and a single currency. Rounding, currencies and ledger double-entry are out of scope.
- **The anti-pattern module is test-only.** `modules/ledger/antipatterns.py` is imported solely by `tests/test_antipatterns.py`; the static auditor reports its two findings on purpose.
- **No observability, rate limiting, pagination or background work.** Those belong to other references of the skill; adding them here would blur the decisions this example demonstrates.
- **Verified on one stack.** See `VERIFICATION.md` for the exact versions; the label `verified` applies to that record, not to other combinations of FastAPI, SQLAlchemy or PostgreSQL major versions.
