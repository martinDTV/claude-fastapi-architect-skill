# FastAPI Architect v2.0.0 — acceptance report (release candidate, 2026-10-31)

Scope: the local commits between the published `3ccc46a` (v1.9.0) and the candidate HEAD, audited as one system:
decision engine, declarative contracts, blueprint and generation, PostgreSQL and Alembic, discovery, safe evolution,
recovery journal, structural transforms, selective adoption, identity and authorization, row-level security, the
static auditor and the verified remediation of v1.9. Every verdict below names its evidence; what was not executed is
marked as such. Nothing was published: no push, tag, release or merge.

Environment: macOS (Darwin 27), Python 3.13.5 (`/opt/anaconda3`) and 3.9.6 (`/usr/bin/python3`), PostgreSQL 16.10
(Homebrew, throwaway clusters under `/tmp`), uv 0.11.28 resolving from the local cache (generated projects run on
CPython 3.13 inside their own virtual environment), ruff 0.17.3 through `uvx`.

## 1. Repository integrity

| Check | Verdict | Evidence |
|---|---|---|
| HEAD / remote | PASS | `origin/main` = `3ccc46a`; candidate commits linear, 0 merges, author and committer `martinDTV <153138284+martinDTV@users.noreply.github.com>` on every commit, no `Co-Authored-By`, no `Signed-off-by` |
| Working tree | PASS | clean before and after the audit; no stash; one worktree |
| Foreign work | PASS | every commit in the range is this project's; consumer repositories untouched (read-only calibration only) |
| Secrets | PASS | scan of every added line of the range (private keys, cloud and API tokens, passwords, certificates): none; placeholders only (`PASSWORD@`, `replace-with-a-generated-key`, `test-key`); no personal paths; the two calibration projects appear by name only, as in v1.7–v1.9 notes |
| Manifests | PASS | `plugin.json` 2.0.0, `marketplace.json` 2.0.0 pointing at `./plugins/fastapi-architect`, `TOOL_VERSION` 2.0.0, audit `SCHEMA_VERSION` 1.7, 59 rules; `claude plugin validate` passes |
| Documentation layout | PASS | root holds `README.md` only; release notes for v1.2–v2.0 and `MERGE-NOTES.md` under `docs/releases/`; architecture record, guides and this report under `docs/`; `docs/README.md` indexes them; the contractual plugin files (`SKILL.md`, `references/`, `evals/`, `.claude-plugin/`) are in place and asserted by `scripts/validate_structure.py` |
| Tracked caches or binaries | PASS | none added; `examples/tenant-ledger/uv.lock` is the v1.5 reference lock, pre-existing |

## 2. Capability matrix

Legend: Impl = implemented, Part = partial, No = not supported; U = unit tested, I = integration tested (generated
project executed through the trust model), PG = verified on a real PostgreSQL with the restricted role, E2E = exercised
by the acceptance scenario (`tests/test_acceptance_v20.py`).

| Capability | Status | U | I | PG | E2E | Limitations | Production risk |
|---|---|---|---|---|---|---|---|
| Recommend an architecture style from a contract (layered, modular monolith, hexagonal, microservices as one monolith per context) with rationale, rejected alternatives, assumptions and pending decisions | Impl | yes | n/a | n/a | yes | rule-based, deterministic; DDD/CQRS/event sourcing only as pending decisions | low: advisory output |
| Validate design contracts 1.0, 1.1, 1.2 strictly with JSON paths and provenance | Impl | yes | n/a | n/a | yes | 1.2-only keys rejected in older documents by design | low |
| Generate a FastAPI backend (app factory, settings, problem details, per-module layers, tests, `.audit-fastapi.json`) | Impl | yes | yes | yes | yes | only declared operations; no Docker/CI; `uv.lock` not generated | low: deterministic, never overwrites without `--overwrite` |
| In-memory persistence | Impl | yes | yes | n/a | yes | process-local store | n/a (development only) |
| PostgreSQL persistence with SQLAlchemy 2 async, naming convention, composite tenant foreign keys | Impl | yes | yes | yes | yes | asyncpg only | medium: operators must own the database and roles |
| Alembic environment, initial migration, additive revisions (tables, nullable or defaulted columns, nullable foreign keys) | Impl | yes | yes | yes | yes | no expand/contract, data migrations, renames, type changes, merges of heads | medium: a downgrade drops added columns with their data |
| API-key authentication (shared key, or grants per key with tenant, subject, roles) | Impl | yes | yes | yes | yes (grants) | shared key has no caller identity | low |
| OIDC bearer-token validation (JWKS over https, asymmetric algorithms, issuer/audience/exp/nbf/iat, claims) | Impl | yes | yes (17 HTTPS scenarios against a local provider) | n/a | yes | no OAuth2 flows, consent, refresh tokens, issuance; not verified against a real provider | medium: provider configuration is the operator's |
| Principal-tenant binding (`api_key_registry`, `principal_claim`) | Impl | yes | yes | yes | yes | tenant path parameter must match the bound tenant | low |
| Per-operation roles and ownership of owned entities (403 / 404 / 503 kept apart) | Impl | yes | yes | yes | yes | business rules beyond the declared policy are the author's | low |
| PostgreSQL row-level security (`shared_schema_rls`): policies ENABLE + FORCE in the creating migration, application role verified not to bypass, transaction-local context, two database URLs | Impl | yes | yes | yes (53 generated tests as `fa_app`, 14 RLS demonstrations) | yes | not against SQL injection or arbitrary SQL as the application role; the throwaway owner is a superuser | medium: role provisioning and two URLs are operational duties |
| Schema or database per tenant | No | — | — | — | — | recorded in the contract, not generated | n/a |
| Audit of generated and existing projects (59 rules, contracts, declared contracts, JSON/SARIF, fingerprints, suppressions) | Impl | yes | yes | n/a | yes | heuristic signals, never verdicts; names never count as authorization | low |
| Project discovery of generated and hand-written projects (static, never imports) | Impl | yes | yes | yes | yes | layout `unknown` for unusual trees | low |
| Evolution of generated projects: add modules, additive changes to existing modules, classified changes | Impl | yes | yes | yes | yes | no removals, renames, type changes, layout migrations; `security`/`persistence` are not evolution operations | medium: integration points are regenerated; hand edits there block |
| Recoverable writer (journal, staged content, backups, rollback/forward, conflicts left alone, unreadable journals block) | Impl | yes | yes | n/a | yes (apply path) | atomic per file, no global atomicity, no durability against power loss (no fsync), same filesystem | medium: a crash mid-publication needs `recover` |
| Structural transforms on adopted files (append handler, append optional field, ensure import) with AST evidence | Impl | yes | yes | n/a | n/a | append-only; never rewrites existing nodes | low |
| Selective adoption of hand-written projects (`propose`, `adopt`, protected unadopted files) | Impl | yes | yes | n/a | n/a | no generated modules or persistence for adopted projects | low |
| Verified remediation (v1.9 recipes, isolated copies, nonce-bound evidence, trust model) | Impl | yes | yes | yes (opt-in, unchanged since v1.9) | n/a | local executor is process hygiene, not a sandbox; untrusted fails closed | medium: the operator authorises execution explicitly |
| Executing generated or audited code | Part | yes | yes | yes | yes | only with `--authorize-execution` and an accepted trust profile, in a copy, through the pytest JUnit adapter | high if authorised on code one does not trust: that is why it is refused by default |

What the skill can do, by verb: **recommend** (advisor, references), **generate** (blueprint and guarded writer),
**execute** (only tests of generated or audited code, only through the trust model), **verify** (static audit always;
executed tests when authorised; PostgreSQL behaviour only in the opt-in suites), **modify** (generated projects by
regeneration of managed files; adopted files by append-only transforms; never files it does not own).

## 3. End-to-end cycle (own fixtures, `tests/test_acceptance_v20.py`)

| Step | In memory (`team-notes`: OIDC, claim binding, roles and ownership) | PostgreSQL (`tenant-projects`: grants, roles and ownership, RLS) |
|---|---|---|
| Requirements | contract 1.2 valid, no refusal, provenance recorded | same |
| Architecture | `layered`, rationale and rejected alternatives, deterministic | `modular_monolith`, same |
| Generation | 31 files, no conflicts | 41 files, no conflicts |
| Audit | 0 findings, `authorization_unknown` 0, `unauthenticated_not_public` 0 | two informational findings, same counters |
| Tests through the trust model | 71 passed, 0 skipped, nonce-bound evidence, project unchanged | 53 passed as `fa_app`, 0 skipped |
| Authorization proven | role, ownership, other tenant 403 and 404, OIDC negatives, JWKS over HTTPS | role, ownership, cross-tenant reference 409, RLS boundary |
| Evolution | module `boards` (owned) planned, verified, applied; identity and policy modules untouched | module `milestones` (owned) planned, verified, applied |
| New migration | n/a | additive revision chained after the initial one, `_protect("milestones")`, `owner_id` column, one head, historical file unchanged |
| Compatibility | every previously passing test still passes (71 before, 93 after); routes 10 → 15; manifest history generate → evolve; every file `managed_unchanged`; repeat is `noop`; audit after 0 | same checks (53 before, 72 after); routes 11 → 15; the chain downgrades to `alembic_version` only |
| Wall time of the whole cycle | 21.5 s | 41.7 s (two `uv sync` from the local cache included) |
| Evidence | `acceptance-team-notes.json` | `acceptance-tenant-projects.json` (with `FA_ACCEPTANCE_EVIDENCE`) |

Defects found by the scenario and fixed: the generated `core/authz.py` named the owned entities in its docstring, so
adding an owned module by evolution required regenerating a file that is not an integration point and the plan was
blocked (`not_allowed_to_modify`); and the generated PostgreSQL conftest refused a database holding only the empty
`alembic_version` table that its own downgrade leaves, so the second suite on the same disposable database (the
evolution step) ended in `infrastructure_error`. Both are fixed in the blueprint, the snapshots were regenerated and the
scenario now runs generation and evolution on one database.

## 4. Architecture evaluation

- Coupling: the design package depends on the audit package through its public entry points (`audit_project`,
  `AuditOptions`, the trust-model `Authorization`, executors, `Workspace`, `compare_reports`, `TOOL_VERSION`) and, in
  discovery, on the resolver and facts internals of the same plugin; the audit package never imports the design
  package. Verdict PASS.
- Contracts: one `Spec` normalises every schema (1.0–1.2); the manifest, the guarantees, the advisor and the planner
  read the same `Spec` helpers (`rls`, `oidc`, `authz_model`). The manifest's `security` record and the report's
  guarantees are two renderings of it; a consistency test now compares them on every snapshot. Verdict PASS.
- Duplication: `TENANT_PARAMS` was defined twice (contracts and the security rules) and is now imported from
  `contracts`; `ROLE_RE`/`ADMIN_RE` remain near-duplicates that only produce named-only labels since the hardening
  (not changed: low value, some risk). Verdict PASS with a note.
- Responsibilities: the blueprint is a pure function; the writer owns every filesystem rule; the journal owns
  recoverability; the auditor is read-only; the executors own execution. No component executes generated code outside
  the trust model. Verdict PASS.
- Inadvertent behaviour changes: measured with the precision script against HEAD on every change of the auditor
  (section 6). Verdict PASS.

## 5. Security and limits

| Check | Verdict | Evidence |
|---|---|---|
| Identity, authorization and RLS hold after generation and evolution | PASS | acceptance scenarios: the policy applies to the new owned module with no extra declaration; `core/security.py`, `core/authz.py`, `core/db.py` are not regenerated by an evolution; the new table is protected in its revision |
| `authorization_call` is not a strong declaration on a name | PASS (fixed) | counts only when the call resolves to a project function that shows a decision, itself or one call deep; otherwise `authorization_named_only`; controls `patch_policy_call` / `patch_policy_call_fake` |
| `decorator_authz` is not a strong declaration on a name | PASS (fixed) | `decorator` only through a known framework origin (`starlette.authentication.requires`) or a visible decision in the resolved project function; controls `patch_decorated_known` / `_fake` / `_external` |
| Untrusted projects refused for local execution by default | PASS | `test_untrusted_authorization_fails_closed`, `test_untrusted_profile_fails_closed`, `test_untrusted_execution_is_denied_and_strict_fails` (design, evolution, CLI); no `--authorize-execution`, no execution |
| `guarantees` and `not_claimed` accurate | PASS | frozen per contract under `tests/fixtures/design20/guarantees`, compared exactly; the RLS block states who sets the context, who can change it, the role's privileges and the owner's exposure |
| Heuristics still named-based, documented as such | NOTE | `principal_with_resource` (the principal flows into the same project call as the id) and `resource_dependency` (a dependency receives the path id) are flow-based, not name-based; authentication itself is recognised by dependency names and chains, which the docs state since v1.6 |

## 6. Compatibility

| Item | Verdict | Evidence |
|---|---|---|
| Python 3.9 and 3.13 | PASS | full suite green on both (section 7) |
| 59 rules, identifiers, audit schema 1.7, fingerprints, suppressions, JSON/SARIF | PASS | `RULES` 59; v1.4–v1.9 test modules green; SARIF of the new fixture validates against the vendored 2.1.0 schema; fingerprints do not depend on effective paths |
| Precision against HEAD (dev and acceptance corpora, calibration projects) | PASS | per-rule tp/fp/fn/tn identical on truth16, truth17 dev/acceptance, truth18 dev/acceptance; plataforma-citas-medicas 52 → 52, agentforge-notify 15 → 15; holdout never re-scored in v2.0 |
| Design contracts 1.0, 1.1, 1.2 | PASS | examples on 1.0 (inventory), 1.1 (orders) and 1.2 (team-notes, tenant-projects); older semantics preserved and tested |
| Evolution 1.0 and 1.1, adoption 1.0, manifest 1.0–1.2 | PASS | accepted and tested; manifests 1.0/1.1 read by discovery; 1.2 emitted |
| Remediation run 1.1 and CLI of v1.9 | PASS | `test_audit_v19` green; `audit_remediate.py`, `audit_diff.py`, `audit_fastapi.py` flags unchanged |
| Plugin installation and discovery | PASS | `claude plugin validate`; marketplace source `./plugins/fastapi-architect`; `validate_structure.py` on both interpreters |
| Snapshots | PASS | the four committed examples equal a fresh generation; regenerated when the blueprint changed |
| Deliberate semantic changes in this range | DOCUMENTED | authorization kinds need visible decisions (names, decorators, policy calls, router-level authentication); three historical test expectations changed with their reasons; corpora unaffected |

## 7. Tests and validations executed

Numbers from the final runs of this acceptance; the per-step logs were kept outside the repository.

| Validation | Result |
|---|---|
| Full suite, Python 3.13 | 551 tests, OK, 11 skipped (the 10 PostgreSQL opt-in tests and ruff); 118.9 s wall, 128 MB max RSS |
| Full suite, Python 3.9 | 551 tests, OK, 24 skipped (plus the generated suites, which need Python 3.12+); 117.8 s wall, 175 MB max RSS |
| PostgreSQL opt-in module (9 tests, restricted role, throwaway cluster) | OK in 111 s |
| PostgreSQL acceptance scenario | OK in 43 s |
| Baseline before the acceptance (HEAD `4f08020`) | 546 tests, OK, 10 skipped; 80.8 s wall, 142 MB max RSS |
| Generated suites through the trust model | team-notes 71, tenant-projects 53 (then 66 after evolution), orders-platform 45 / 58 / 46, inventory-service 10 |
| HTTPS/JWKS scenarios | 17 in `tests/test_jwks.py` of every OIDC project |
| Evolution and recovery | 26 + 18 + 22 tests; corrupt and incomplete journals added |
| Acceptance scenarios | 3 tests (in-memory cycle, PostgreSQL cycle, manifest/guarantees consistency) |
| Adversarial controls | named-authorization fixture (8 negative, 8 positive, 2 router-level, 3 decorators, 2 policy calls; 13 tests), security negative controls, RLS boundary, fault injection |
| Ruff on the four generated projects | clean |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate`, docs links, JSON/SARIF, secrets scan, `git diff --check` | clean |
| Offline evals | 48 cases structurally validated; no paid run |

## 8. Recovery and consistency

| Scenario | Verdict | Test |
|---|---|---|
| Failure before publication | PASS | `test_precondition_failure_before_publication_changes_nothing`, `test_write_error_during_staging_leaves_nothing_behind` |
| Failure during a multi-file publication | PASS | `test_failure_after_two_files_is_rolled_back_or_rolled_forward` |
| Failure after some files landed / all landed | PASS | same, `test_failure_after_everything_landed_only_needs_cleanup` |
| Failure during recovery | PASS | `test_interrupted_recovery_can_be_resumed_and_is_idempotent` |
| File altered concurrently | PASS | before publication aborts; during recovery the file is left alone and reported |
| Journal incomplete or corrupt | PASS (fixed) | was silently ignored; now blocks plans and is reported as `needs_attention` with staged files kept |
| Operation already completed | PASS | `test_retry_of_a_completed_operation_is_recognised_by_its_identity` |
| Ownership preserved, foreign code never overwritten | PASS | unmanaged and unadopted files protected; manual changes preserved or blocking; `test_unsafe_paths_and_symlinks_are_refused_before_staging` |
| Documentation claims | PASS | atomic per file, recoverable across files, no global atomicity, no durability against power loss (stated now explicitly) |

## 9. Blocking and non-blocking defects

| Defect | Severity | Status |
|---|---|---|
| Unreadable journal ignored by the planner and by `recover` | blocking for the recovery guarantee | fixed, tested |
| Evolution adding an owned module blocked by a docstring in `core/authz.py` | blocking for the evolution of 1.2 projects with ownership | fixed, snapshots regenerated, covered by the scenario |
| Generated PostgreSQL suites refused a database that only held the empty `alembic_version` table a previous run's downgrade leaves behind (a second run on the same disposable database failed before testing) | blocking for the reproducibility of the cycle on one database | fixed in the generated conftest (the empty version table is tolerated, any other table still refuses), snapshots regenerated, covered by the PostgreSQL scenario (generation and evolution on the same database) |
| `decorator` and `authorization_call` kinds granted on names | non-blocking (uncertainty understated) | fixed, controls added |
| `TENANT_PARAMS` duplicated | cosmetic | fixed |
| `ROLE_RE` / `ADMIN_RE` near-duplicate regexes | cosmetic | left, noted |

## 10. Residual risks

- The auditor sees what the project contains: a policy implemented in a library, behind a dynamic call or a decorator
  it does not know is reported as unknown, never as absent and never as present.
- Row-level security protects against missing filters and wrong ids, not against SQL injection reaching statement
  level or arbitrary SQL as the application role; the generated code only issues parameterised statements.
- The local executor is process hygiene; authorising execution of code one does not trust remains the operator's
  decision, refused by default.
- PostgreSQL behaviour is verified on a throwaway cluster whose owner is a superuser; a non-superuser owner in
  production is subject to FORCE and needs a context or BYPASSRLS for data migrations.
- Durability: writes are renamed into place, not synced; a power loss in the middle is a `recover` case, not a
  guarantee.

## 11. Verdict

**READY** as a release candidate, pending the author's authorisation to publish: every blocking defect found during
the acceptance was fixed and re-verified, the guarantees are frozen and match the executed evidence, and the limits
are written where the user will read them. Nothing was pushed, tagged or released.
