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
