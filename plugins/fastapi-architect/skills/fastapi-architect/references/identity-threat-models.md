# Identity, authorization and threat modeling

## Trigger
Auth flows, token verification, tenant boundaries, service calls, resource grants, patient/financial data, uploads, webhooks and external identity providers.

## Start with a threat model
List assets; actors; trust boundaries (browser, API gateway, worker, database, object store, external provider); attacker goals; entry points; controls; testable negative cases. Record whether control is preventive, detective, or recovery.

## JWT access token acceptance
1. Pin supported algorithms and verify signature against provider-specific keys. Never select verification algorithm from untrusted header.
2. Resolve issuer from trusted provider discovery/configuration; an issuer may be shared across apps. Compare exact `iss` and validate the token kind and required claims.
3. Validate `aud` according to the **provider's access-token contract**, not a generic assumption. If provider uses another binding such as `client_id`, validate it too against explicit local application ID. Reject unexpected/missing claims per documented provider behavior.
4. Validate `exp`, `nbf` where applicable, reasonable `iat` and time skew, plus required subject/session IDs.
5. Protect JWKS retrieval against insecure URLs/SSRF and unknown issuers; cache with bounded freshness and honor key removals within the security policy. Define controlled behavior on provider outages.
6. Distinguish authn (401), authz (403/404 according to anti-enumeration policy), enrollment and step-up. Never reveal whether an inaccessible resource exists.
7. Treat logout/revocation as a separate property from JWT expiry: identify session invalidation and whether the backend needs an external revocation check or short TTL.

Do not log raw tokens or Authorization headers. Tests may inspect claims only via safe synthetic fixtures.

## Authorization models
- RBAC: coarse capabilities. ABAC: contextual properties. ReBAC: explicit relationships among actor, resource, organization and delegated access. Many systems need a combination.
- Authorize each use case and each object at the server; frontend navigation checks do not grant permission.
- Design permission evaluation under revocation: caching can widen privileges until TTL expires. Document worst-case window and invalidation strategy.
- Distinguish organizational administrators from owners of sensitive domain records (medical, employment, financial).
- Check delegation scope, expiry, actor provenance and impersonation audit. Break-glass requires explicit policy, logging and scrutiny.

## Multi-tenant boundaries
- Bind principal to allowed organization; never trust arbitrary `X-Tenant-ID` alone.
- Enforce per-row isolation using tenant-filtered queries or transaction-scoped RLS, with tests against missing tenant context, wrong tenant, pooled connection reuse, and bypass roles.
- Distinguish tenant-level metadata from cross-tenant platform administration; audit access as a separate capability.
- Cache keys and exported object paths must incorporate tenant+identity+scope. Invalidate on tenant switch and revocation.

## Files, webhooks and egress
- Signed upload URLs must bind user, tenant, object key, content constraints, expiration and scope of operation.
- Verify webhook signatures against original raw bytes, event timestamp/replay window and unique event ID; acknowledge only according to chosen durable-ingest contract.
- Egress to user-controlled URLs needs DNS/IP and redirect safety, not only a string-based domain allowlist.
- Rate limit auth, account recovery and sensitive mutations separately; never rely on CORS as a security boundary.

## Negative tests
Tampered JWT; accepted token from wrong app but same issuer; revoked grant; changed tenant; missing RLS context; upload into another tenant prefix; expired webhook replay; unknown JWT kid; disconnected JWKS; session expiration during long request; inconsistent authorization across API and background worker.
