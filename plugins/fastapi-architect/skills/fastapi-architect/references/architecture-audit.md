# Architecture audit of an existing FastAPI codebase

## Trigger
The user asks for an audit, diagnosis, architecture review, "what should we fix first", a refactor of a codebase they did not just write, or a second opinion on a FastAPI repository. Also use it before any large refactor, and whenever a conclusion about an existing system is about to be stated as fact.

An audit is read-only until the user asks for changes. It produces evidence, a classified list of findings, a comparison with the recommended architecture, and an incremental plan. It does not produce a rewrite.

## Workflow

1. **Inventory** the repository without modifying it.
2. **Run the static auditor** for triage signals (`scripts/audit_fastapi.py`).
3. **Map layers and dependencies** from imports, not from folder names.
4. **Check the antipattern catalogue** against the code, in impact order.
5. **Compare** the observed architecture with the recommended stage.
6. **Classify** every finding with a severity, a confidence and an evidence state.
7. **Reproduce** the findings that matter, as tests where feasible.
8. **Report** with the template below, including what was not verified.
9. **Plan an incremental refactor**, one reversible change at a time.
10. **Protect with regression tests** before and after each change.
11. **Close** findings only against the acceptance criteria.

## 1. Initial inventory

Read, in this order: `pyproject.toml` or `requirements*.txt` and the lockfile (versions actually installed), `CLAUDE.md`/`AGENTS.md`/ADRs, the entry point (`create_app`, lifespan, router registration), settings, DB session setup, migrations folder, the test suite and CI configuration, container and deployment files. Then the folder tree:

```bash
find . -name "*.py" -not -path "*/.venv/*" -not -path "*/node_modules/*" | sort | head -200
grep -rn "async def\|^def " --include="*.py" src | wc -l
grep -rn "create_engine\|sessionmaker\|async_sessionmaker" --include="*.py" .
grep -rn "HTTPException" --include="*.py" src | grep -v "router\|api/" | head
```

Record facts, not impressions: Python and framework versions, number of modules and endpoints, whether tests exist and what they hit (SQLite, real PostgreSQL, mocks), whether migrations exist, how the app is deployed. Do not run anything against production data or real credentials.

## 2. Static signals: `scripts/audit_fastapi.py`

The skill ships a read-only auditor based on the standard-library `ast` module. It parses Python files, never imports or executes them, writes nothing unless `--output` is given, skips `.git`, virtualenvs, caches, `node_modules`, `dist`, `build` and dot-directories, does not follow symlinked directories and analyses each real file once.

```bash
python3 scripts/audit_fastapi.py /path/to/project              # text summary
python3 scripts/audit_fastapi.py /path/to/project --json       # JSON report
python3 scripts/audit_fastapi.py /path/to/project --output audit.json --fail-on high
python3 scripts/audit_fastapi.py --list-rules
```

| Rule | Severity | Signal | What it does **not** prove |
|---|---|---|---|
| `ASYNC_BLOCKING_CALL` | medium | `time.sleep`, `requests.*`, `urllib.request.urlopen`, `subprocess.run/check_output`, `os.system`, sync `httpx.*` helpers, sync DB `connect` called directly in an `async def`, with import aliases resolved and local shadowing respected | That the call is on a hot path, or that it blocks for long; an intentional call in a startup hook may be acceptable |
| `ASYNCIO_RUN_NESTED` | high | `asyncio.run(...)` or `loop.run_until_complete(...)` inside an `async def` | That the function is ever executed under a running loop; confirm the receiver is an event loop when confidence is `medium` |
| `ROUTE_NO_RETURN_ANNOTATION` | info | Route decorator without a return annotation and without `response_model`/`response_class` | A defect; the route may return a `Response` or be covered elsewhere. Never report this as a bug |
| `PY_SYNTAX` | low | File does not parse with the running Python; other rules did not run on it | That the file is shipped; it may be a template or legacy script |
| `FILE_SKIPPED` | info | Unreadable file or above the size limit | Anything about the content |

The report is JSON with `schema_version`, `tool` (name, version and the Python version whose grammar parsed the files), `project_root`, `files_scanned`, `files_skipped` (with a reason each), `summary` (counts plus `partial_analysis`), `findings` (each with `rule`, `severity`, `confidence`, `file`, `line`, `column`, `message`, `symbol`, `function`) and a `disclaimer`. Output is deterministic for the same tree and interpreter, so two runs can be diffed. Exit codes: 0, 1 when a finding reaches `--fail-on`, 2 for a missing path or an unwritable `--output`.

Run the auditor with the Python version the project targets. An older interpreter reports modern syntax as `PY_SYNTAX`, marks the analysis partial, and leaves those files unanalysed by the other rules.

Test code and documented suppressions (schema 1.1): each finding carries `environment` (`application` or `test`). Test findings are reported, with one confidence level less for blocking calls, because the stalled loop is the test's own; pass `--exclude-tests` only when the audit scope is production code, and say so in the report. A finding on a line with `# noqa: ASYNC2xx` or `# audit-fastapi: ignore[RULE]` stays listed as `suppressed` and is excluded from `--fail-on`; treat it as "accepted by the team, verify the justification" rather than as absent. `--no-suppressions` shows everything as active.

Treat every auditor finding as **"suspected, backed by evidence"** at best. Open the file, confirm the context, then classify. Known blind spots are listed in section 14.

Since v1.4 the auditor also covers SQLAlchemy sessions and engines (`SA_*`), transactions and concurrency (`TX_*`), Alembic migrations (`MIG_*`), authorization and multi-tenancy signals (`SEC_*`) and architecture boundaries (`ARCH_*`). The full catalogue, with what each rule proves, what it cannot and how to verify it, is `references/audit-rules.md`. Useful options for an audit:

```bash
python3 scripts/audit_fastapi.py <project> --format markdown > audit.md        # report to attach
python3 scripts/audit_fastapi.py <project> --category migrations,transactions  # focus
python3 scripts/audit_fastapi.py <project> --baseline previous.json             # new / fixed / unchanged
python3 scripts/audit_fastapi.py <project> --changed-files changed.txt          # review only a diff
```

Each schema 1.2 finding carries `category`, `verification` (`static`, `needs_runtime_check`, `not_verifiable_statically`), a `recommended_test` and a `fingerprint` stable across line shifts. Since v1.5 (schema 1.3) it also carries `evidence` (`verified`, `inferred`, `unknown`) and, for cross-function findings, a `call_path`; the report's `interprocedural` block states how many call sites were resolved and whether the pass was truncated. Quote the call path in the report as the evidence trail, and treat `inferred` findings as suspicions to confirm by reading the functions in that path (`references/audit-engine.md`). Since v1.6 (schema 1.4) the `interprocedural` block also states `coverage.project_call_coverage` (how many project calls resolved), `classified_ratio` (how much code the engine could say anything about), `uncertainty.ambiguous_sites` (several candidates, never guessed) and `limits`; quote the coverage in the report so the reader knows how far the static claims reach, and treat `ANALYSIS_PARTIAL` findings and `truncated: true` as gaps to list under "not verifiable with the available information". `--time-budget` raises the budget when a large repository truncates. Since v1.7 (schema 1.5) the `contracts` block lists every route with its authentication and authorization kinds and whether it is public by declaration; quote its summary (`unauthenticated_not_public`, `authorization_unknown`) in the security section and treat each `authorization_unknown` route as a question with a negative test, never as a finding by itself. Map `evidence_state` to the evidence states of section 6 directly: `static_confirmed` is "suspected, backed by evidence" (confirm by reading), `inferred` the same with lower confidence, `insufficient` is "not verifiable with the available information", `partial` lists a gap, `suppressed` is "accepted by the team, verify the justification"; `reproduced` only appears in records of runtime tests, never in the auditor's output. For CI, `--sarif audit.sarif` feeds code-scanning dashboards and `--baseline` plus `--fail-on-new` gates on regressions; declared `contracts` in `.audit-fastapi.json` turn the project's own rules (authenticated prefixes, mutations with visible authorization, forbidden imports, commit layers) into findings. Map `verification` to the evidence states of section 6: `static` findings can be confirmed by reading; `needs_runtime_check` findings stay "suspected" until a measurement or a database test; `not_verifiable_statically` findings are questions that only a negative or concurrent test can answer. The `facts` block says what the tool inferred (layers, business-modules root, session factories, auth signals, import-linter contracts) so the report can state its assumptions explicitly.

## 3. Identifying layers and dependencies

Folder names lie; imports do not. Determine the real structure from import edges:

```bash
grep -rhn "^from app\|^import app" --include="*.py" src | sed 's/:.*from /: /' | sort | uniq -c | sort -rn | head -50
uv run lint-imports            # if import-linter is configured
```

Map each file to a role (HTTP boundary, use case / service, persistence, domain, infrastructure, worker, CLI) by what it imports and what imports it, not by its name. Then answer:

- Is the top level organised by business capability or by technical type (`routers/`, `models/`, `services/`)?
- Which direction do dependencies flow? Any persistence or domain code importing FastAPI or `HTTPException`?
- Who owns the transaction? Repositories committing, routers committing, or services?
- Which modules read other modules' tables or ORM models directly? Any cross-module FK relationships?
- Where are engines, sessions, HTTP clients and settings created: lifespan, module import time, or per call?
- Do workers, CLIs and webhooks go through the same services as HTTP, or bypass them?

Capture the result as a small table: module, role, imports from, imported by, owns tables, transaction owner. That table is the backbone of the comparison in section 5.

## 4. Antipattern catalogue

Check in impact order. "Evidence" is what must be observed before the item is reported.

| Antipattern | Evidence that confirms it | Typical impact | Fix direction |
|---|---|---|---|
| Blocking I/O in `async def` | Call site plus the library being sync (driver, SDK, `requests`), on a request path | Cross-request latency, worker stalls | Async client, `anyio.to_thread.run_sync`, or plain `def` |
| Engine/session created at import time or shared globally | `create_engine`/`Session()` at module scope, session stored on a module global | Connection leaks, cross-request state, test flakiness | Lifespan + per-request dependency |
| Business rules in routers | Branching on domain state, calculations or multi-step writes in a route body | Untestable without HTTP, duplicated in workers | Move to service; router parses, calls, maps |
| Services raising `HTTPException` or importing FastAPI | Import in service module | Services unusable from workers/CLI | Domain exceptions + global handlers |
| Cross-module model or repository imports | Import edge between modules not through `public.py` | Extraction impossible, hidden coupling | Public facade, ID references |
| ORM objects returned directly | Route without response schema returning a model instance | Data leaks, lazy-load errors | `XRead` schema or `response_model` |
| `create_all()` in production, no migrations | Call site in startup, missing `alembic/` | Unrepeatable schema, drift | Alembic with naming convention |
| Scattered `os.getenv`, secrets in code or images | grep hits outside settings | Misconfiguration, leaks | One `Settings`, `SecretStr` |
| CORS `*` with credentials | Middleware config | Credential exposure | Explicit origins |
| Tenant ID taken from the request body or query | Query filter built from client input without membership check | Cross-tenant access | Tenant from verified identity; RLS fail closed |
| `BackgroundTasks` for work that must survive a restart | Side effects with money, email, external state | Lost work on deploy or crash | Outbox + worker |
| Webhooks/retries without idempotency | Handler writes without dedup key | Duplicate side effects | Inbox table, idempotency key |
| Tests hitting real external services or no DB-backed tests | Fixtures, URLs, credentials in tests | Flaky, unsafe, unverifiable | Fakes for providers, real PostgreSQL in CI |
| Over-engineering | Interfaces with one implementation and no fake, DI containers, generic repositories bypassed by raw queries | Cost without benefit | Remove the layer, keep the seam that pays rent |
| Growing `core/`/`utils/` with business logic | Domain terms inside infrastructure packages | Hidden module, unbounded coupling | Move into the owning module |

An item that is only a naming or layout preference is **style**, not a finding. Report it separately and never with a severity above `info`.

## 5. Current versus recommended architecture

Determine the stage the project is in and the stage it should be in using the criteria in `SKILL.md` and `references/decision-router.md`. Then compare dimension by dimension:

| Dimension | Observed | Recommended for this project | Gap | Cost to close | Priority |
|---|---|---|---|---|---|
| Top-level organisation | by type | by business capability | … | … | … |
| Module boundaries | enforced / informal / none | import-linter contracts | | | |
| Transaction ownership | … | service | | | |
| Async model | … | consistent, measured | | | |
| Error handling | … | domain exceptions + Problem Details | | | |
| Configuration | … | one `Settings` | | | |
| Migrations | … | Alembic | | | |
| Tests | … | unit + real PostgreSQL + contract | | | |
| Observability | … | request IDs, tracing, health | | | |
| Deployment | … | immutable image, non-root | | | |

The recommendation must fit the team and the requirements the user actually has. A coherent existing convention that differs from the skill's default is not a gap. A gap with no observed cost is low priority even if the skill's default says otherwise.

## 6. Classifying findings

Each finding carries three independent labels.

**Category**: functional defect, security, reliability/performance, maintainability, style. Keep them apart: a missing return annotation is maintainability or style; a blocking call on a hot path is reliability; a tenant filter built from client input is security. Do not inflate style into defects or defects into vulnerabilities.

**Severity**: `high` (data loss, cross-tenant access, outage under normal load), `medium` (degradation, incorrect behaviour in realistic cases, expensive future change), `low` (localised inefficiency or fragility), `info` (observation, no action required).

**Evidence state**, exactly one of:

| State | Meaning | What you may say |
|---|---|---|
| **Observed and reproduced** | The behaviour was triggered with a test, command or trace in this environment, and the output is recorded | "This fails: here is the test and its output." |
| **Suspected, backed by evidence** | Code reading or a tool points to it (file and line), but it was not executed | "The code at X does Y, which would cause Z. Not yet reproduced." |
| **Not verifiable with the available information** | Depends on runtime configuration, data, infrastructure or a dependency not accessible from the repository | "Cannot confirm without A. Here is what to check." |
| **Fixed and verified** | A change was applied and the reproduction from state 1 now passes, with the regression test kept | "Fixed in commit X; the test that failed now passes." |

A finding moves between states only with new evidence. Static-auditor output alone never reaches "observed and reproduced". A fix without a failing-then-passing check never reaches "fixed and verified"; call it "applied, not verified".

**Confidence** (`high`/`medium`/`low`) expresses how likely the claim is to be correct given the evidence; it is not severity. A `high`-severity, `low`-confidence item is reported as a question to investigate, not as a defect.

## 7. Evidence and confidence

Acceptable evidence: file and line, the import or configuration that makes the behaviour certain, a command and its output, a failing test, a trace or log excerpt, a query plan, a measured latency. Weak evidence: folder names, docstrings, comments, the presence of a library in the lockfile, memory of how a framework "usually" behaves.

State which commands ran and their outcome. If a tool was unavailable or a check was skipped, say so in the report. Do not assume versions, runtime flags, worker counts or database settings; read them or mark the finding not verifiable.

## 8. Reproducing defects

Prefer a reproduction that becomes a regression test. Minimal recipes:

- **Blocking call in async**: a test that starts the app with `httpx.AsyncClient`, fires the suspect endpoint concurrently with a trivial endpoint, and asserts the trivial one is not delayed by the blocking duration. Or measure event-loop lag with a periodic task during the call.
- **Nested `asyncio.run`**: call the coroutine inside a running loop in a test; expect `RuntimeError`.
- **Lost update**: two independent sessions update the same row; without a version check both succeed and one write is lost.
- **Cross-tenant access**: two tenants, authenticate as A, request B's resource by ID; expect 404/403 and no row. Repeat for list, search, aggregate, join, background job and raw SQL paths.
- **Import-time connection**: import the app module with the database unreachable; it must not fail or connect.
- **Missing migration**: run `alembic upgrade head` on an empty database, then `alembic check` or autogenerate; a non-empty diff is the finding.
- **Cross-module coupling**: an import-linter contract that fails.

Keep the reproduction small, deterministic, free of real data and of network access to third parties. If a reproduction needs infrastructure that is not available, record the exact steps and leave the finding in state 2 or 3.

## 9. Incremental refactor

Refactor only what the user asked for and only after the reproduction exists. Order by risk reduction per unit of change, not by how satisfying the final structure looks.

1. Add characterization tests for the behaviour that must not change (contract snapshot of OpenAPI, key end-to-end flows, DB-backed tests for the touched transactions).
2. Make one boundary explicit at a time: extract a service from a router, introduce `public.py` for one module, move one engine into the lifespan.
3. Keep each change independently deployable and reversible. One pull request per seam. No mass renames mixed with behaviour changes.
4. Enforce the boundary mechanically as soon as it exists (import-linter contract) so it does not regress while the rest is migrated.
5. Write an ADR for anything expensive to reverse: sync to async, splitting a module into a service, changing the auth provider, changing transaction ownership.
6. Never block the team: migration paths coexist with the old structure until the last caller moves. Delete the old path in a separate change.

A proposal to "move everything to the recommended layout" is not a plan. A plan names the first three changes, their tests, their rollback and the finding each one closes.

## 10. Regression tests

Before a fix: a test that fails for the reported reason. After a fix: the same test passes, the rest of the suite still passes, and the OpenAPI snapshot is unchanged unless the change was meant to alter the contract. For data changes, run migrations up and down on a disposable PostgreSQL. For security findings, keep the negative test (the request that must be refused) permanently.

If the project has no test harness, the first refactor step is the harness for the touched path, not the refactor. Say so explicitly when the user asked for the refactor directly.

## 11. Technical report

Use this structure; omit nothing, mark unknowns as unknown.

1. **Scope and method**: what was reviewed, how (commands, tools, versions), what was excluded, time box.
2. **Inventory**: stack and versions, size, modules, deployment, test posture.
3. **Findings**, ordered by severity then confidence. Each with: ID, title, category, severity, confidence, evidence state, location (file:line), evidence, impact, recommended fix, alternative, verification step, estimated cost.
4. **Architecture comparison**: the table from section 5, stage now versus stage recommended, with the reasoning.
5. **Incremental plan**: the first three to five changes, each tied to finding IDs, tests and rollback.
6. **Style and conventions**: separate list, `info` only.
7. **Limitations**: what was not verified, blind spots of the tools used, assumptions made.
8. **Commands executed** and their results, verbatim where short.

Do not include secrets, personal data or large file excerpts in the report. Reference locations instead.

## 12. Acceptance criteria

A finding is closed when all of the following hold: it was reproduced (or explicitly accepted as not reproducible with a documented reason), a change was applied, the reproduction now passes, the full suite and the static checks pass, the regression test is kept, and the change is documented (changelog, ADR when applicable). Partial progress is reported as partial.

An audit is complete when every finding has a state, the comparison table is filled, the plan exists, limitations are written down, and the user can act on it without asking what was actually checked.

## 13. Authorization and multi-tenancy audit

High-risk area; apply `references/authz-multitenancy.md` and `references/identity-threat-models.md` for the full model. The audit must check, with evidence for each:

- Every command and query that touches protected data enforces authorization server-side, through one shared policy path used by HTTP, workers, webhooks and CLI.
- Tenant identity comes from verified credentials or membership, never from a client-supplied `tenant_id` alone.
- With PostgreSQL RLS: tenant context is set transaction-locally, the application role cannot bypass RLS, context is absent by default, and nothing leaks across pooled connections.
- Lists, searches, aggregates, joins, UPSERTs, raw SQL, background jobs and exports apply the same tenant scope as single-resource reads.
- Admin and support paths are explicit, audited capabilities with least-privilege roles.
- JWT validation checks signature, algorithm, issuer, audience or client binding, expiry and required claims; JWKS rotation and key removal are handled.
- Negative tests exist for horizontal access, vertical access, enumeration and revoked or foreign tokens.

Report each item with an evidence state. "No cross-tenant test exists" is a finding in itself. Never conclude that a system is "secure"; conclude which specific checks passed, which failed, and which could not be verified.

## 14. Limits of static tools

The auditor and any grep-based check share these blind spots; list the relevant ones in every report:

- Calls through instances, attributes or dynamically resolved names (`self.client.get`, ORM sync sessions, SDK clients) are not resolved. Absence of `ASYNC_BLOCKING_CALL` findings does not mean the event loop is free of blocking work.
- No inter-procedural analysis: a sync helper that blocks, called from an `async def`, is invisible.
- Decorator-based route detection is heuristic; routes registered with `add_api_route` or custom decorators are missed, and non-FastAPI decorators may be misread when the receiver is named `router`.
- Runtime configuration, worker model, middleware order, dependency versions, database settings and deployment topology are not visible in source.
- Syntax that the running interpreter does not understand is reported as `PY_SYNTAX`, not analysed.
- Generated code, vendored dependencies and templates can produce noise; exclude them deliberately and say so.
- A static tool cannot observe data, load, latency or incidents. Findings about performance or security remain in state 2 or 3 until exercised.

Static output is triage. The audit's value comes from the verification, classification and plan built on top of it.
