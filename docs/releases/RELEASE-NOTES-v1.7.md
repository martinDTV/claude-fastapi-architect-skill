# FastAPI Architect v1.7 — Verified Security, Architecture Contracts & Reliability Engineering

Additive release on top of v1.6.0 (`068b849`). No tracked file from earlier versions was removed or renamed; the plugin keeps the identity `fastapi-architect`, the `/fastapi-architect` invocation, every CLI option, every rule id, the suppression markers and the fingerprints. Report schema 1.5 is a superset of 1.0–1.4: it adds the top-level `contracts` block, `evidence_state` per finding, `summary.by_evidence_state` and the `contracts` category.

## Requirements of the brief

| Requirement | Status | Where |
|---|---|---|
| Baseline before changes | complete | saved reports and metrics of v1.6 (tests, rules, schema, evals, example, calibration, corpus) |
| Independent corpus first, rules second; adversarial; comparison with v1.6 | complete | `tests/fixtures/truth17/{dev,acceptance,holdout}`, `scripts/audit_precision.py --against 068b849` |
| Authentication contracts (Depends, Security, factories, aliases, decorators, guards, authorization services, resource-principal relations, composed dependencies, facades) | complete | `fastapi_audit/contracts.py`, `contracts` block, six-bucket partition |
| Authorization by resource, membership, tenant, role; public routes declarable; unknown as an outcome | complete | contract kinds; `public_routes`, `# audit-fastapi: public`; `SEC_RESOURCE_AUTHZ_UNVERIFIED` as a question |
| Multi-tenancy and RLS: requested, authenticated and authorized tenant; context; propagation; PostgreSQL tests | complete | `SEC_RLS_CONTEXT_FROM_REQUEST`, `tenant_params`, `tenant-ledger/tests/test_tenant_binding.py` |
| Resource verification: sessions, clients, pools, transactions, locks, background tasks, coroutines | complete for coroutines, locks, HTTP clients, sessions and tasks; pools/transactions rely on the v1.4–v1.6 rules | `rules/lifecycle.py` |
| Idempotency, retries, outbox and inbox | complete (inbox added now as a question) | `REL_CONSUMER_WITHOUT_INBOX` plus the v1.5/v1.6 retry, idempotency and outbox rules |
| Declarative, opt-in architectural contracts | complete | `contracts` in `.audit-fastapi.json`, `rules/declared.py`, four `CONTRACT_*` rules |
| SARIF 2.1.0 for CI | complete | `--format sarif`, `--sarif FILE`, `report.to_sarif` |
| Explicit evidence classification: verified, inferred, unknown, partial, suppressed | complete | `evidence_state`: `static_confirmed`, `inferred`, `insufficient`, `partial`, `suppressed`; `reproduced` reserved |
| Compatibility of CLI, JSON, fingerprints, suppressions | complete | contract tests; fingerprints identical on both calibration repositories |
| Executable examples with PostgreSQL | complete | 29 tests on a throwaway cluster |
| Validation of precision, performance, security, determinism | complete | this document and the test suites |
| Identity propagation into workers beyond the first hop; authorization through ambient state; SQL tenant filters inside services | pending (backlog) | `references/audit-rules.md` |

## Baseline (Phase 0)
Git at `068b849`, version 1.6.0, 210 tests green, 48 rules, schema 1.4, 39 evaluation cases, `tenant-ledger` at 27 tests. Calibration repositories (read-only): Clivana 55 findings, AgentForge 11. Corpus `truth16`: precision, recall and F1 1.0, project-call coverage 0.929.

## Independent corpus and measurements (Phase 1)
`tests/fixtures/truth17` holds three projects whose expectations were written before the detectors they measure, as inline markers (optionally with the expected evidence class) and in an `EXPECTATIONS.md` each:

- `dev` (16 files, 64 markers): used during implementation. Two changes after the first write are logged in its change log: a marker that expected silence on a shared session inside test code contradicted the policy in force since v1.3 (test findings are classified, never hidden), and three consumer cases were added for a rule the first write did not anticipate; no existing expectation changed.
- `acceptance` (13 files, 33 markers, a clinic domain): held out, scored at the first release candidate, never edited.
- `holdout` (14 files, 34 markers, a logistics domain declaring its tenant parameter and architectural contracts): written on 2026-10-30 before the evidence states, SARIF, declared contracts and the consumer signal were implemented; scored once with the final engine, never edited.

`scripts/audit_precision.py` reports precision, recall, F1, false positive rate (reported negatives over documented negatives), false negative rate, unknown-evidence rate and the verified/inferred/unknown split, and runs any previous commit in a separate process.

| Corpus | v1.6.0 | v1.7.0 | Notes |
|---|---|---|---|
| dev, precision / recall / F1 | 0.684 / 0.684 / 0.684 | 1.0 / 1.0 / 1.0 | FPR 0.136 → 0.0; FNR 0.316 → 0.0; unknown-evidence rate 0.19 (the five questions) |
| acceptance, original first run | 0.778 / 0.538 / 0.636 | 0.900 / 0.692 / 0.783 | as first measured, before any change prompted by it |
| acceptance, after one engine fix | same | 0.786 / 0.846 / 0.815 | the fix: `staff`/`manager` were privilege words (`Depends(current_staff)` counted as role authorization) |
| acceptance, with `tenant_params: ["clinic_id"]` declared (temporary copy) | — | 0.929 / 1.0 / 0.963 | the remaining item is an expectation that contradicts the test-code policy |
| holdout, single run with the final engine | 0.875 / 0.368 / 0.519 | 0.895 / 0.895 / 0.895 | FPR 0.067 → 0.118; FNR 0.632 → 0.105; unknown-evidence rate 0.211 |

Honest reading of the held-out partitions. Acceptance exposed one engine defect, fixed and disclosed above; for the two rules it touched the acceptance set is no longer strictly held out. Its two "false negatives" are the undeclared domain tenant (`clinic_id`), which a project declares with `tenant_params`, and its one "false positive" is a marker that expected silence on a shared session in test code. Holdout, scored once and left as is, has four open items: `GET /api/status` is treated as a health-style public path by its own path, so neither `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` nor the declared `route_auth_required` fires (an engine limitation: public-by-path looks at the route path, not the full path); `POST /api/shipments/{id}/reroute` receives both the tenant question and the resource question (the engine asks twice about one route; the expectation foresaw one); and `POST /api/shipments/{id}/close` also violates the declared mutation contract, which the expectation omitted (the engine is right). No held-out expectation was changed; the dev corpus was not used to calibrate against either held-out set.

## Security contracts (Phase 2)
`fastapi_audit/contracts.py` builds one contract per route; the report carries them in `contracts` with a disjoint six-bucket `partition`: `public_declared`, `unauthenticated_not_public`, `optional_authentication` (the dependency admits anonymous callers), `authenticated_authorization_visible`, `authenticated_only` (authenticated, no authorization kind visible, and the route acts on no resource or tenant id), `authenticated_authorization_unknown` (authenticated, no kind visible, acts on a resource or tenant id). Authentication is recognised through dependency chains up to three levels, permission factories (`require(perm)`, `needs(scope)`), class-based checkers (`RoleChecker("admin")`, `Permission("x")`), `Security(scopes=...)`, router and application dependencies (inherited by every route), middleware and decorators such as `@requires`. Authorization kinds: `role`, `scopes`, `decorator`, `membership`, `resource_dependency`, `principal_with_resource`, `authorization_call`, `tenant_comparison`, `router_dependencies`. Declarations: `public_routes`, `# audit-fastapi: public`, `tenant_params`. The v1.4 quirk that made `@router.get("")` on a prefixed router "public" is gone. `staff` and `manager` are no longer privilege words.

Why Clivana reads 167 / 167 / 127 / 2: every one of its 167 routes carries an authentication signal (permission dependencies built with `require(p.X)` through `AccessContext`, membership dependencies, or router-level dependencies), so `authenticated` is 167; 127 routes additionally show an authorization kind (role/permission dependency, membership, principal flowing with the resource, a policy call), 38 are authenticated and act on no resource or tenant id (`authenticated_only`, for example `/me`-style and list endpoints whose scope comes from the context object), and 2 are the health-style `/live` and `/ready`; `authenticated_authorization_unknown` is 0. The categories are independent: authentication says who the caller is, authorization kinds say what relates the caller to the action or the resource, public says the project declared it. Absence of a visible authorization kind is never reported as a vulnerability; the rules ask a question only in the last bucket.

## Multi-tenancy and RLS (Phase 3)
`SEC_RLS_CONTEXT_FROM_REQUEST` (medium, `static_confirmed`, runtime check): the RLS context is set, directly or through a project call, from a tenant id taken from the request in an authenticated route that does not bind it to the principal; it replaces the generic tenant-binding question for that route. The reference implementation reproduces it on PostgreSQL (a member of ACME reads Globex's accounts through correctly configured policies when the context comes from the URL; the real membership dependency returns 404) and shows that a worker handed only a tenant id writes into that tenant.

## Resource lifecycle, idempotency and consumers (Phases 4–5)
`ASYNC_COROUTINE_NOT_AWAITED` (high), `ASYNC_LOCK_PER_CALL` (medium), `REL_HTTP_CLIENT_PER_CALL` (low), `REL_CONSUMER_WITHOUT_INBOX` (low, question), `SEC_RESOURCE_AUTHZ_UNVERIFIED` (medium, question). `SA_SESSION_SHARED_CONCURRENTLY` no longer reports a task created with the session and awaited right away. Retries, idempotency keys, lost responses, outbox and inbox are covered together with the v1.5/v1.6 rules; the dev, acceptance and holdout corpora each contain the positive and the guarded negative.

## Declared contracts, evidence states, SARIF (remaining phases)
- `contracts` in `.audit-fastapi.json` (JSON only, read with `json.load`; a test proves no project Python is imported or executed, and malformed declarations are ignored): `route_auth_required`, `mutation_authorization_required`, `forbidden_imports`, `commit_layers` → `CONTRACT_ROUTE_AUTH_REQUIRED`, `CONTRACT_MUTATION_AUTHZ_REQUIRED`, `CONTRACT_IMPORT_FORBIDDEN`, `CONTRACT_COMMIT_OUTSIDE_LAYER` (category `contracts`; nothing fires without a declaration).
- `evidence_state` per finding and `summary.by_evidence_state`: `suppressed` > `partial` > `insufficient` > `inferred` > `static_confirmed`; `reproduced` is in the enumeration for runtime records and is never produced by the tool. A semantic pattern is never presented as a proof of exploitation.
- SARIF 2.1.0: `--format sarif` and `--sarif FILE`; `$schema` is the OASIS 2.1.0 errata01 schema; one run, the rules used, one result per finding with level from severity, `%SRCROOT%` locations, the stable fingerprint as a partial fingerprint, in-source suppressions, result properties (confidence, evidence, evidence_state, verification, call path, recommended test), `baselineState` with `--baseline`, and run properties with the summaries. Tests check the required structure, the correspondence with the JSON report and determinism; on Clivana the document has 10 rules, 56 results and one suppressed result, byte-identical across runs.

## Calibration on real repositories (read-only, working trees unchanged)

| | Clivana (457 files) | AgentForge (288 files) |
|---|---|---|
| Findings v1.6 → v1.7 | 55 → 55 | 11 → 11 |
| Partition | authorization visible 127, authenticated only 38, public 2, unknown 0 | visible 6, authenticated only 6, public 2, unknown 5 |
| Evidence states | static_confirmed 52, inferred 2, insufficient 1, suppressed 1 (AgentForge: 11 static_confirmed) | |
| Audit time (measurement harness, back-to-back) | 11.4 s → 11.8 s | 6.5 s → 7.0 s |
| Peak RSS | 118 MB → 96 MB | 59 MB → 61 MB |

One finding appeared and was removed during this review: `REL_CONSUMER_WITHOUT_INBOX` on an OAuth redirect handler named `receive_authorization` with `dedupe_key=` in a keyword argument; the consumer heuristic now requires a message-shaped name and parameter and counts keyword argument names as guards. The eleven existing fixtures and `truth16` are unchanged against v1.6; the example trades the generic tenant question for the RLS-context finding on its deliberate route.

## Tests
239 in the plugin (210 from v1.6 plus 29 in `tests/test_audit_v17.py`: dev corpus scored in-process, contracts block and partition, declarations, optional authentication, inherited router dependencies, resource kinds, each new rule, the spawn-then-await refinement, configurable tenant params, declared contracts on `tests/fixtures/contracts_declared`, configuration-is-data, SARIF structure and correspondence, evidence states, schema 1.5, CLI determinism) and 29 in the example. Suites pass on Python 3.13 and 3.9 and under pytest.

## Evals
Eight cases added (47 total): declaring public routes, the resource-authorization question with a ContextVar check, the unbound RLS context, a dropped coroutine, a per-call client with a per-call lock, a Rails/Pundit negative, a CI gate on SARIF and evidence states, and declaring architectural contracts. Validated structurally; no paid runs.

## Compatibility
New report block `contracts`, new per-finding field `evidence_state`, new summary key `by_evidence_state`, new category `contracts` (schema 1.5, additive). New configuration keys `public_routes`, `tenant_params`, `contracts`; new marker `# audit-fastapi: public`; new CLI output `--format sarif` and option `--sarif`. Ten rules added (58 in total); none removed or renamed; fingerprints of unchanged findings identical on both calibration repositories; v1.3-style suppressions honoured.

## Third review (2026-10-30): corrections before publication
Historical figures above are preserved as measured; the held-out partitions were not re-scored. Three corrections and one interoperability validation:

1. **Configuration fails closed.** `config_schema.validate_config` checks `contracts`, `public_routes` and `tenant_params` (types, required fields, admitted values, incompatible rules, invalid globs, unknown properties unless `x-` prefixed) and `audit_project` raises `ConfigError`; the CLI prints every problem with its JSON path and the file name and exits 2 without a report. A configuration without these sections behaves exactly as before; older sections stay lenient. Tests: valid declarations pass, each error class fails, `--config custom.json` names the file, project Python is still never executed.
2. **Route exposure.** A health-check naming convention no longer makes a route `public`: the partition gains `exposure_undetermined`, `public_declared` counts declarations only, the informational `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` stays silent on convention paths, and a declared `route_auth_required` contract is never overridden by the convention. The example now declares `/health` in its `.audit-fastapi.json`. On the calibration repositories the two health routes of each moved from "public" to `authenticated_only` (they carry authentication), so Clivana reads 127 / 40 / 0 and AgentForge 6 / 8 / 5 (visible / authenticated only / unknown); no finding changed.
3. **One question per route.** The tenant questions and the privilege question subsume `SEC_RESOURCE_AUTHZ_UNVERIFIED` on the same route (same root cause, more specific evidence); independent risks keep both. A parameter named `role`/`permissions` is the value being assigned, not a policy signal (it had silenced a privilege question). Messages and fingerprints are unchanged: shared findings on Clivana (55/55) and AgentForge (11/11) keep identical fingerprints.
4. **SARIF validated against the official schema.** `tests/schemas/sarif-schema-2.1.0.json` is the OASIS SARIF 2.1.0 errata01 schema (SHA-256 prefix `c3b4bb2d60938974`, 112,768 bytes, downloaded 2026-10-30). `scripts/validate_sarif.py` validates with `jsonschema` 4.23.0 (draft-04 semantics, as the schema declares) and adds structural checks; `tests/test_sarif_schema.py` validates a document with a partial result, an in-source suppression and a baseline. Results: Clivana (55 results, 1 suppressed), AgentForge (11) and the example (6) are schema-valid, structurally sound and byte-identical across runs; `baselineState` is now emitted for every result when a baseline is given (`new` or `unchanged`). Interoperability limits are documented in `references/audit-engine.md`. Exit codes for audits without declared contracts are unchanged.

Regression fixtures: `tests/fixtures/regressions_v17` (convention versus declared contract, one question per route) and the configuration tests in `tests/test_audit_v17.py`; 251 tests in the plugin, 29 in the example.

## Known limitations
- Authorization carried by ambient state (ContextVars, `request.state`), by middleware the project does not contain, or inside unresolved services stays `unknown`.
- Public-by-path looks at the route's own path and is only a convention: `/status` under an API prefix is `exposure_undetermined`, the informational rule stays silent, and a declared `route_auth_required` contract reports it.
- A route with both a tenant id and a resource id receives the tenant question only; the resource id stays listed in its contract.
- Domain tenant names must be declared (`tenant_params`).
- `reproduced` is never produced statically; the reference implementation's tests are the only records of that state in this repository.
- SARIF: vendor fingerprint key only, `baselineState` limited to `new`/`unchanged`, in-source suppressions only (see the engine reference).
- The acceptance partition is no longer strictly held out for the two rules affected by the disclosed vocabulary fix; the holdout partition was scored exactly once.
