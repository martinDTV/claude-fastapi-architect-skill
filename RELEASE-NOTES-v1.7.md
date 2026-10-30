# FastAPI Architect v1.7 — Verified Security, Architecture Contracts & Reliability Engineering

Additive release on top of v1.6.0 (`068b849`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id, the suppression markers and the fingerprints. Report schema 1.5 is a superset of 1.0–1.4: the only addition is the top-level `contracts` block.

## Baseline (Phase 0)
Git at `068b849`, version 1.6.0, 210 tests green, 48 rules, schema 1.4, 39 evaluation cases, `tenant-ledger` at 27 tests. Calibration repositories (read-only): Clivana 55 findings, AgentForge 11. Corpus `truth16`: precision, recall and F1 1.0, project-call coverage 0.929. All baselines were saved before any change.

## Independent corpus first, rules second (Phase 1)
`tests/fixtures/truth17` holds two projects written before the v1.7 detectors existed, with expectations as inline markers (optionally with the expected evidence class) and in `EXPECTATIONS.md`:

- `dev` (15 files, 61 markers): safe, unsafe and ambiguous cases, negative controls, known false positives, permission factories, composed dependencies, RLS context, resource authorization, HTTP retries, lost responses, concurrent transactions, idempotent operations, background tasks, a correctly shared HTTP client, an incorrectly shared session, outbox and inbox. Used during implementation.
- `acceptance` (13 files, 33 markers, a clinic domain with different shapes): held out, scored once at release time, never edited.

`scripts/audit_precision.py` now also reports false positive rate, false negative rate, unknown-evidence rate, the verified/inferred/unknown split and evidence mismatches. One change was made to `dev` after writing and is logged in its `EXPECTATIONS.md`: a marker that expected silence on a shared session inside test code contradicted the policy in force since v1.3 (test findings are classified, never hidden).

| Corpus | Metric | v1.6.0 | v1.7.0 |
|---|---|---|---|
| dev | precision / recall / F1 | 0.684 / 0.684 / 0.684 | 1.0 / 1.0 / 1.0 |
| dev | false positive rate / false negative rate | 0.136 / 0.316 | 0.0 / 0.0 |
| dev | unknown-evidence rate | 0.211 | 0.15 (the three questions) |
| acceptance, as written | precision / recall / F1 | 0.778 / 0.538 / 0.636 | 0.786 / 0.846 / 0.815 |
| acceptance, with the project's tenant parameter declared (measurement on a temporary copy) | precision / recall / F1 | — | 0.929 / 1.0 / 0.963 |

The acceptance run exposed one engine defect, fixed and disclosed: `staff` and `manager` were privilege words in the role vocabulary, so `Depends(current_staff)` counted as role authorization and silenced two questions. After the fix the acceptance set is no longer strictly held out for those two rules. The remaining open items on acceptance are not engine defects: two routes use `clinic_id` as the tenant, which a project declares with `tenant_params` (without it the engine asks the resource-authorization question instead), and one marker expected silence on a shared session in test code (the same policy error as in dev, left in place because the acceptance corpus is not edited).

## Security contracts (Phase 2)
`fastapi_audit/contracts.py` builds one `RouteContract` per route and the report carries them in `contracts`: authentication (dependency chains up to three levels through aliases, facades, permission factories such as `require(perm)`, class-based checkers such as `RoleChecker("admin")`, `Security(scopes=...)`, router and application dependencies, decorators such as `@requires`), authorization kinds (`role`, `scopes`, `decorator`, `membership`, `resource_dependency`, `principal_with_resource`, `authorization_call`, `tenant_comparison`, `router_dependencies`), public by declaration (health-style path, `public_routes` globs in `.audit-fastapi.json`, `# audit-fastapi: public`), principal, tenant and resource parameters, and the project calls that receive a resource id without the principal. Authorization unknown is a legitimate outcome, counted in the summary and never a finding by itself. `tenant_params` declares domain tenant names. A collection endpoint on a prefixed router (`@router.get("")`) is no longer "public" (a v1.4 quirk that hid 14 routes in one real repository).

## Multi-tenancy and RLS (Phase 3)
`SEC_RLS_CONTEXT_FROM_REQUEST` (medium, evidence verified, runtime check): the RLS context is set, directly or through a project call, from a tenant id taken from the request in an authenticated route that does not bind it to the principal; it replaces the generic tenant-binding question for that route. The reference implementation reproduces it on PostgreSQL: with correctly configured policies on a non-owner role, a member of ACME reads Globex's accounts when the context comes from the URL; the real membership dependency returns 404 instead. A second test shows that a worker handed only a tenant id writes into that tenant: the binding belongs where the principal is known.

## Resource lifecycle (Phase 4)
`ASYNC_COROUTINE_NOT_AWAITED` (high): a project coroutine resolved through the call graph (including `self.method()` and instances) that is a bare statement, assigned to a name never awaited or scheduled, or returned from an `async def`. `ASYNC_LOCK_PER_CALL` (medium): a lock constructed inside the function that acquires it. `REL_HTTP_CLIENT_PER_CALL` (low): an httpx client built per call, used and neither managed, closed, returned nor stored. `SA_SESSION_SHARED_CONCURRENTLY` no longer reports a task created with the session and awaited right away. `SEC_RESOURCE_AUTHZ_UNVERIFIED` (medium, evidence unknown): an authenticated route hands a resource id to project code without the principal and no authorization kind is visible.

## Calibration on real repositories (read-only, working trees unchanged)

| | Clivana (457 files) | AgentForge (288 files) |
|---|---|---|
| Findings v1.6 → v1.7 | 55 → 55 | 11 → 11 |
| Routes in `contracts` | 167: authenticated 167, public 2, authorization visible 127, unknown 0 | 19: authenticated 19, public 2, authorization visible 6, unknown 5 |
| Audit time (measurement harness, back-to-back) | 16.6 s → 17.1 s | 9.5 s → 9.8 s |
| Peak RSS | 98.6 MB → 98.0 MB | 59.6 MB → 57.6 MB |

No new finding on either repository; the eleven existing fixtures and `truth16` are unchanged against v1.6.

## Tests
226 in the plugin (210 from v1.6 plus 16 in `tests/test_audit_v17.py`: dev corpus scored in-process, contracts block, declarations, each new rule, the spawn-then-await refinement, configurable tenant params, schema 1.5, CLI determinism) and 29 in the example. Suites pass on Python 3.13 and 3.9 and under pytest.

## Evals
Six cases added (45 total): declaring public routes, the resource-authorization question with a ContextVar check, the unbound RLS context, a dropped coroutine, a per-call client with a per-call lock, and a Rails/Pundit negative. Validated structurally; no paid runs.

## Compatibility
New report block `contracts` (schema 1.5). New configuration keys `public_routes` and `tenant_params`; new inline marker `# audit-fastapi: public`. Five rules added (53 in total); none removed or renamed; fingerprints of unchanged findings identical (verified on both calibration repositories).

## Known limitations
- Authorization carried by ambient state (ContextVars, `request.state`), by middleware the project does not contain, or inside services the engine cannot resolve stays `unknown`; the contract says so instead of guessing.
- Domain tenant names must be declared (`tenant_params`); without the declaration the engine treats `clinic_id`-style path ids as resources.
- `SEC_RLS_CONTEXT_FROM_REQUEST` follows one call level into project code; a context set three calls deep is not seen.
- The acceptance partition is held out for every rule except the two affected by the vocabulary fix described above.
- The brief for this version reached Phase 4; the remaining phases followed the pattern of previous releases (evaluations, documentation, validation, versioning, local commits) and stop before publication.
