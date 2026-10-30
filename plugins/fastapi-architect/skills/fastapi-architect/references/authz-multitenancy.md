# Security, authorization, identity, multitenancy

## Threat-model each protected operation
Assets, identities, trusted boundaries, attack surfaces, abuse incentives, and failure outcomes. Test horizontal and vertical access, IDOR/BOLA, enumeration, rate limits, input limits, replay, SSRF, upload scanning, and supply chain. Use OWASP ASVS/API Security Top 10 as audit inputs, not an assertion of compliance.

## Authentication
- Validate JWT signature against trusted algorithm/key set; exact issuer, audience/client binding as defined by the actual provider token profile, required claims, expiry and clock skew. Never decode a token without verification for authorization decisions.
- JWKS cache: key-id lookup, refresh limits, failure policy, removed-key invalidation, rotation tests and protection from arbitrary `jku`/`x5u` URL fetching.
- Session revocation can require server-side state even when JWT verifies cryptographically. MFA at login is not necessarily recent, operation-bound step-up.
- Refresh token and browser architecture decisions must account for cookie partitioning, CSRF, XSS and deployment domains. BFF vs SPA must be justified by deployment topology rather than a universal mandate.

## Authorization
- Model action, subject, resource, tenant and context. RBAC for coarse access, ABAC/ReBAC for ownership, scope, explicit relationship and resource-specific restrictions.
- UI permissions are for UX only. Enforce every command and query on server; use one tested policy path for API, worker, webhook and CLI if they access protected data.
- Deny by default; resource-scoped and tenant-scoped queries fail closed. Avoid permission oracles: map nonexistent and inaccessible resources according to documented enumeration policy.
- Caches of permissions require explicit invalidation or bounded staleness and a known revocation SLA. Do not treat short-lived cache as instantaneous revocation.

## Tenant isolation
- Authenticate tenant membership; never accept `tenant_id` merely because user supplied it.
- For PostgreSQL RLS set tenant context transaction-locally (`SET LOCAL` or `set_config(..., true)`), use a role without BYPASSRLS or owner bypass, ensure tenant context is absent by default and reset between pool reuse.
- Test adversarial queries, joins, UPSERT, aggregates, async jobs, search, raw SQL, foreign keys, security-definer functions, and background operations. Multi-tenant constraints must protect cross-tenant references.
- Shared admin/service paths require audited, explicit capabilities and least-privilege DB roles.

## Privacy and secrets
- Minimize PII in URLs, request logs, traces, exception messages, metrics labels, caches, exports and test fixtures. Treat request bodies as confidential by default.
- Secrets through secret manager/env, not VCS, images or public runtime config. Rotate using overlapping key rings with verification, migrate dependent encrypted material, and retain needed historic verification evidence.
- Signed URLs: short TTL, strict object/purpose binding, file size/content type restrictions, malware scanning workflow, safe filenames, retention/deletion policy.

## What static analysis can and cannot say about authorization (v1.4)

The auditor's `security` category reports review signals, never verdicts:

- `SEC_TENANT_ID_FROM_REQUEST`: a route uses a tenant/organisation identifier from the request and no authentication or membership dependency is visible on the handler, its router, the route decorator or the application. A gateway, middleware in another repository, or a composed dependency can make this a false positive; a cross-tenant negative test (member of A asks for B's identifier: single resource, list, search, aggregate) is what settles it.
- `SEC_RLS_SET_NOT_LOCAL`: `SET app.tenant = ...`, `SET ROLE` or `set_config(..., false)` persists on the pooled connection after the transaction. The integration test in the skill repository shows the value surviving a commit on the same connection; `SET LOCAL` / `set_config(..., true)` do not.
- `SEC_ROUTE_WITHOUT_AUTH_SIGNAL`: informational; the route may be public by design.

Not verifiable statically, and therefore never claimed by the tool: that a visible `Depends(get_current_user)` binds the tenant to that user; that RLS policies exist on every table, including join and audit tables; that the application role lacks `BYPASSRLS` and is not the table owner; that background jobs run with an explicit tenant context. Each of those is a negative test or a database query (`pg_policies`, `pg_roles`), listed in `references/testing-matrix.md`. Silence from the auditor is not a security result.

### v1.6: what the auditor adds and what it still cannot say
- Authentication reached through composed dependencies (`ctx = Depends(get_context)` where `get_context` depends on `get_current_user`) is recognised by following the chain; the absence of a function named `authorize` is never the evidence of a missing check.
- `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` is a question (`evidence: unknown`): the route is authenticated, takes a tenant id from the request and uses it, and no visible step binds that tenant to the principal (no membership dependency, the principal does not flow where the tenant goes, no comparison between the two). Middleware, row-level security keyed on the principal or a check inside the service make it a false positive; a cross-tenant negative test settles it either way.
- `SEC_PRIVILEGE_CHANGE_UNVERIFIED` is a question too: roles, permissions or admin flags change in a route whose only visible signal is generic authentication. Name the policy (an admin/owner dependency, an explicit check in the body, a decorator dependency) and test it as a plain member.
- Still outside static reach: hard-coded permissions, identity propagation into workers beyond the first hop, and whether row-level security policies are keyed on the right setting.

### v1.7: contracts, resource authorization and a tenant check that is present but not bound
- The report's `contracts` block classifies each route: authentication present or absent, authorization kinds visible (role, scopes, decorator, membership, resource dependency, principal flowing with the resource, authorization-service call, tenant comparison, router dependencies), public by declaration. Declare intentionally public routes (`public_routes`, `# audit-fastapi: public`) and domain tenant names (`tenant_params`) so the contract reflects the design.
- `SEC_RESOURCE_AUTHZ_UNVERIFIED` is a question: the id reaches a service without the principal and nothing visible relates them. Make the relation explicit (pass the principal or a context object, use a resource dependency, call the policy where both are known) or prove it with the negative test and document the suppression.
- `SEC_RLS_CONTEXT_FROM_REQUEST` is the "present but not bound" check: RLS is configured and the context is set, but from the tenant the caller named. The reference implementation shows a member of ACME reading Globex's rows through correctly configured policies, and the membership binding that returns 404 instead. `organization_id` in the URL is not isolation; membership before context is.
- A tenant id alone never authorizes a worker or a background task: RLS cannot know who asked. Bind tenant and principal where the principal is known, then hand the bound tenant to the worker.
