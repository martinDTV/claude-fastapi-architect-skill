# Plugin evals for fastapi-architect

Eval suite in the native `claude plugin eval` format (`<case>/prompt.md` or `<case>/case.yaml` plus `graders/*.md`). It measures three things: whether the skill is invoked when it should be and stays quiet when it should not, the quality of architectural decisions and diagnostics, and whether the shipped static auditor is actually used when the session can run Python.

The human-scorer prompts in the repository root `evals/*.json` are unchanged; this directory is what `claude plugin eval` executes.

## Layout

```
plugins/fastapi-architect/evals/
├── README.md
├── <case>/prompt.md              # prompt + frontmatter (tags, max_turns, allowed_tools)
├── <case>/graders/*.md           # llm rubric, regex keyword, tool_used indicators
├── audit-fixture-run-auditor/, detect-session-risks/
│   ├── case.yaml                 # inline prompt, graders and context.scaffold_script
│   ├── scaffold.sh               # copies fixtures/sample_app into the run workspace (runs only with --scaffold)
│   └── fixtures/sample_app/      # tiny FastAPI project with known defects (test data, never shipped code)
└── results/                      # written by runs; git-ignored
```

## Cases

| Case | Tags | What it checks |
|---|---|---|
| `audit-blocking-triage` | positive, diagnosis | Blocking calls in `async def`: impact explained, verification proposed before asserting severity, no overclaiming |
| `audit-not-secure-claim` | positive, diagnosis | Refuses to certify "secure"; lists concrete tenant-isolation checks; UI filtering is not authorization |
| `audit-nested-asyncio-run` | positive, diagnosis | Correct cause of `asyncio.run()` RuntimeError, await as the fix, reproduction test, no `nest_asyncio` by default |
| `audit-route-annotation-not-a-defect` | positive, diagnosis | `ROUTE_NO_RETURN_ANNOTATION` stays informational; `response_model` already covers routes; prioritised by risk |
| `audit-legacy-by-type-layout` | positive, decision | Incremental refactor plan with a safety net first; modular monolith, not microservices; style versus defect |
| `decision-microservices-split` | positive, decision | Does not split; names the missing hard criteria; proposes module boundaries and what would change the decision |
| `reference-impl-verification-label` | positive, decision | Complete If-Match example, two-writer negative test, declared versions, labelled not verified when nothing ran |
| `audit-fixture-run-auditor` | positive, tools | Runs `scripts/audit_fastapi.py` on a fixture, verifies findings, classifies with evidence states (needs `--scaffold --allow-tools Bash`) |
| `negative-flask-structure` | negative | Flask project organisation: the skill must not fire |
| `negative-asyncio-script` | negative | Plain asyncio script, no web framework: the skill must not fire |
| `negative-express-api` | negative | Express.js API structure: the skill must not fire |
| `reject-false-positive-sessionmaker` | positive, diagnosis (v1.4) | A global `async_sessionmaker` with a yield dependency is correct; the answer must distinguish factory from instance |
| `propose-concurrency-test` | positive, diagnosis (v1.4) | Lost-update suspicion: two-writer reproduction before locking, with alternatives (atomic UPDATE, FOR UPDATE, version) |
| `destructive-migration-justified` | positive, decision (v1.4) | A justified `MIG_DROP_COLUMN`: approve with evidence and checks, not block or rubber-stamp |
| `security-not-verifiable-statically` | positive, diagnosis (v1.4) | `SEC_TENANT_ID_FROM_REQUEST` with `CurrentUser` injected: no yes/no from code; cross-tenant negative test |
| `justify-existing-architecture` | positive, decision (v1.4) | Hexagonal layout with import-linter contracts: do not reorganise into the default modular layout |
| `select-reference-zero-downtime-migration` | positive, decision (v1.4) | 80M-row type change and NOT NULL column: expand-contract, batches, CONCURRENTLY, rehearsal |
| `detect-session-risks` | positive, tools (v1.4) | Runs the auditor on a fixture with a shared session, a lost update and a commit-in-loop; must reject the sessionmaker false positive (needs `--scaffold --allow-tools Bash`) |
| `negative-django-project` | negative (v1.4) | Django REST Framework restructuring: the skill must not fire |
| `trace-session-across-modules` | positive, diagnosis (v1.5) | Untyped session flowing route → service → repository; interprocedural reasoning, un-awaited commit, typed boundary |
| `indirect-blocking-helper` | positive, diagnosis (v1.5) | Blocking call two calls below an async route; call path, offload/async fixes, no "more workers" |
| `shared-session-taskgroup` | positive, diagnosis (v1.5) | Session shared across TaskGroup tasks: not a SQLAlchemy bug; session per task; HTTP clients differ |
| `async-lazy-loading` | positive, diagnosis (v1.5) | MissingGreenlet from an implicit lazy load; loader options, lazy="raise" guard |
| `non-idempotent-retry` | positive, decision (v1.5) | Retried payment POST: keep the retry, add idempotency key and reconciliation |
| `background-tasks-request-session` | positive, diagnosis (v1.5) | Request session handed to BackgroundTasks: lifetime, session inside the task, outbox for durability |
| `tenant-propagation-worker` | positive, decision (v1.5) | Tenant and principal context into a worker: captured in the payload, recreated per transaction, negative tests |
| `lost-response-double-charge` | positive, diagnosis (v1.5) | Double charge after a timeout: idempotency at API, database and provider; reconciliation |
| `incremental-refactor-session-handling` | positive, decision (v1.5) | Module-level Session instance: incremental migration to factory + dependency, no rewrite |
| `decision-not-add-complexity` | positive, decision (v1.5) | 6-endpoint tool: decline repository/UoW/CQRS/event bus; name the signals that would justify them |
| `negative-sqlalchemy-etl-script` | negative (v1.5) | Standalone SQLAlchemy ETL script, no web framework: the skill must not fire |
| `negative-nodejs-prisma-transactions` | negative (v1.5) | Node.js + Prisma lost update: the skill must not fire |
| `indirect-dependency-through-attribute` | positive, diagnosis (v1.6) | Un-awaited `self._session.execute` in a method reached three modules away: confirms the defect, explains how the attribute's type is known statically, asks for the test |
| `insufficient-evidence-tenant-binding` | positive, diagnosis (v1.6) | `SEC_TENANT_NOT_BOUND_TO_PRINCIPAL` with `evidence: unknown`: authentication is not binding; a question settled by a cross-tenant test, not a verdict |
| `apparently-insecure-but-correct-shared-client` | positive, diagnosis (v1.6) | A shared `httpx.AsyncClient` and an in-process dispatcher writing through the same session are correct; session versus client, dual write versus same transaction |
| `double-charge-after-retry` | positive, diagnosis (v1.6) | A non-idempotent payment applied twice after a client timeout: idempotency key with a unique constraint in the same transaction, replay, concurrent duplicates, proof |
| `authorization-through-abstractions` | positive, diagnosis (v1.6) | Policy objects behind a composed dependency: legitimate; a tool must follow the chain and report insufficient evidence, tests still required |
| `decision-modular-monolith-over-microservices` | positive, decision (v1.6) | Four modules sharing tables, five people, 200 rps: boundaries first, extraction later under stated conditions |
| `adversarial-unbounded-retry-defended` | positive, adversarial (v1.6) | Pressure to suppress `RETRY_UNBOUNDED` and `ASYNC_TASK_REFERENCE_DROPPED` as "theoretical": declines with the documented behaviour and quick checks |
| `negative-django-celery-task-retries` | negative (v1.6) | Django + Celery retry configuration: the skill must not fire |
| `contract-public-route-declared` | positive, decision (v1.7) | Intentionally public routes are declared (`public_routes`, marker), the webhook keeps signature verification, no blanket globs |
| `resource-authorization-question` | positive, diagnosis (v1.7) | `SEC_RESOURCE_AUTHZ_UNVERIFIED` with a ContextVar check: why static reading differs, make the relation explicit or prove it with a negative test |
| `rls-context-present-but-unbound` | positive, diagnosis (v1.7) | Correct RLS, context set from the URL tenant: the check is present but not bound; membership before context, cross-tenant proof |
| `coroutine-never-awaited-silent-failure` | positive, diagnosis (v1.7) | Dropped coroutine through `self.record`: warning not exception, how the graph knows, `-W error` and linters |
| `shared-client-and-per-call-lock` | positive, diagnosis (v1.7) | Per-call httpx client and per-call lock: both real; lifespan client, shared lock, database guarantee for the transfer |
| `negative-rails-pundit-authorization` | negative (v1.7) | Rails + Pundit authorization coverage: the skill must not fire |
| `ci-sarif-gate-evidence-states` | positive, decision (v1.7) | CI gate on SARIF: fail on new `static_confirmed` high findings, review `insufficient` questions, never fail on them; baselines and suppressions |
| `declarative-architecture-contracts` | positive, decision (v1.7) | Opt-in `contracts`: which rules to declare first, why JSON and not executed config, how to roll out without blocking |

Graders per positive case: `quality` (LLM rubric, weight 2, concrete PASS/FAIL conditions that penalise unsupported claims), `keyword` (regex, weight 1, one technical term the answer must contain) and `skill-fired` (`tool_used: Skill`, `arm: with-only`). In a with/without run the `skill-fired` grader is reported as a plugin-fired indicator and does not count towards the score; with `--ablation none` it is scored. Negative cases carry a single free grader, `skill-not-fired` (`tool_used: Skill` with `min: 0`, `max: 0`).

## Activation criteria (verifiable)

The skill **must** be invoked when the request concerns a FastAPI backend and asks for any of: creating or restructuring a project; an audit, review or diagnosis of existing code; an async or concurrency problem (blocking calls in `async def`, event-loop errors); an authorization or multi-tenancy review; interpretation of a static-analysis finding about FastAPI code; a refactor plan; a runnable FastAPI example. This holds for short questions and when the repository cannot be read. Verified by `skill-fired` passing on every `positive` case.

The skill **must not** be invoked for other frameworks (Flask, Django, Express) unless the work is a migration to FastAPI, nor for generic Python or asyncio questions with no FastAPI backend. Verified by `skill-not-fired` passing on every `negative` case.

Acceptance for a release: every positive case fires and scores 1.0, every negative case does not fire, in a with-plugin run.

## Running

From the repository root. Each run is a full Claude Code child session on your own credential; costs are list-price estimates printed by the tool.

```bash
# Full suite, with and without the plugin (default ablation), 1 run per case
claude plugin eval plugins/fastapi-architect --runs 1 --trust-plugin --no-publish \
  --scaffold --allow-tools Bash --max-cost-usd 15 --json results.json

# Cheaper: with-plugin arm only
claude plugin eval plugins/fastapi-architect --ablation none --runs 1 --trust-plugin --no-publish \
  --scaffold --allow-tools Bash --max-cost-usd 8

# Only activation checks
claude plugin eval plugins/fastapi-architect --tag negative --ablation none --runs 1 --trust-plugin --no-publish
claude plugin eval plugins/fastapi-architect --tag positive --ablation none --runs 1 --trust-plugin --no-publish \
  --scaffold --allow-tools Bash

# One case
claude plugin eval plugins/fastapi-architect --case audit-fixture-run-auditor --ablation none --runs 1 \
  --trust-plugin --no-publish --scaffold --allow-tools Bash
```

Notes:
- `--case` takes a single name or glob per invocation (several `--case` flags do not accumulate; the last one wins). Use `--tag` or a glob such as `--case "audit-*"` to run a group.
- `--scaffold --allow-tools Bash` is only needed for the `tools` cases (`audit-fixture-run-auditor`, `detect-session-risks`). `--scaffold` runs the case's `scaffold.sh` as you, outside the sandbox, before the agent starts; the script only copies `fixtures/sample_app` into the run's empty workspace (read it before trusting it, as with any scaffold). Without the copy the sandboxed shell cannot read fixture directories, and without the Bash grant the `auditor-run` grader fails by design. A narrower pattern such as `Bash(python3:*)` was tried and left the model unable to run anything (its first shell call is usually a lookup, not `python3`), so grant the whole tool; the eval sandbox still confines it.
- Child sessions start with a clean configuration. If your Claude Code authenticates through a cloud provider with application default credentials, export the provider's credentials variable (for Google Cloud, `GOOGLE_APPLICATION_CREDENTIALS` pointing at your own ADC file) before running. Never commit that file or its path.
- Results land in `--output-dir` or in `evals/results/<timestamp>/` (git-ignored). `--json <file>` keeps the machine-readable run; `--keep-temp` preserves per-run traces.
- Local validation of the case files without spending anything: `python3 -m unittest discover -s tests -p "test_plugin_evals.py" -v`.

## Baseline (2026-10-28, before the description change)

Six original cases, 1 run per case, with/without plugin, default model, judge haiku. Cost 5.84 USD, 423 s.

| Case | With | Without | Delta | Skill fired |
|---|---|---|---|---|
| audit-blocking-triage | 1.00 | 1.00 | 0 | no |
| audit-not-secure-claim | 1.00 | 1.00 | 0 | no |
| audit-legacy-by-type-layout | 1.00 | 0.33 | +0.67 | yes |
| audit-nested-asyncio-run | 1.00 | 1.00 | 0 | no |
| audit-route-annotation-not-a-defect | 1.00 | 1.00 | 0 | no |
| reference-impl-verification-label | 1.00 | 1.00 | 0 | yes |

Reading: quality passed everywhere, but the skill fired in 2 of 6 cases. The four question-style diagnostics were answered from general knowledge. The description was rewritten afterwards (see `RELEASE-NOTES-v1.3.md`).

## Confirmation (2026-10-28, after the description change)

With-plugin arm only (`--ablation none`), 1 run per case, default model, judge haiku. The two positives that already fired in the baseline were not re-run. Total cost of the confirmation runs 4.45 USD.

| Case | Score | Skill fired | Notes |
|---|---|---|---|
| audit-blocking-triage | 1.00 | yes | previously not fired |
| audit-not-secure-claim | 1.00 | yes | previously not fired |
| audit-nested-asyncio-run | 1.00 | yes | previously not fired; first confirmation run failed the quality rubric because clause 3 demanded the words "expects RuntimeError before the fix" although the answer proposed the regression test under a running loop; clause rewritten to its intent, re-run passed |
| audit-route-annotation-not-a-defect | 1.00 | yes | previously not fired |
| decision-microservices-split | 1.00 | yes | new case |
| audit-fixture-run-auditor | 1.00 | yes | new case; first runs showed that `Bash(python3:*)` denied the shell and that `add_dirs` is not shell-readable; switched to `scaffold.sh` + `--scaffold --allow-tools Bash`; the final run ran the auditor and reproduced the defects in-process |
| negative-flask-structure | 1.00 | no | correct |
| negative-asyncio-script | 1.00 | no | correct |
| negative-express-api | 1.00 | no | correct |

Grader revisions are listed here on purpose: a rubric that fails a correct answer on wording is a grader defect, and the change is recorded so the baseline and the confirmation are not read as the same instrument.

## Limitations

- One run per case is indicative, not statistically stable; use `--runs 3` before trusting a delta.
- The LLM judge defaults to haiku; pin `--model` and `--judge-model` for comparable runs.
- The with/without comparison cannot show value on prompts the base model already answers well; such cases still guard against regressions.
- Fixture code under `audit-fixture-run-auditor/fixtures/` intentionally contains defects and is excluded from any claim about the plugin's own code quality.

## v1.4 additions (2026-10-28)

Eight cases cover the v1.4 auditor categories: real risks on a fixture (`detect-session-risks`), a false positive to reject, an existing architecture to defend, a concurrency test to propose, a justified destructive migration, security that cannot be verified with AST alone, selection of the database-evolution guidance, and a Django negative. Structural validation (`tests/test_plugin_evals.py`) covers all nineteen cases without cost. Paid runs for v1.4 were a four-case sample (with-plugin only, one run each): `negative-django-project` 1.00 (not invoked), `reject-false-positive-sessionmaker` 1.00, `detect-session-risks` 1.00 and `security-not-verifiable-statically` 1.00 after one rubric revision each (the judge had scored wording instead of substance; the revision is recorded in the case files). Total 2.16 USD. See `RELEASE-NOTES-v1.4.md`.

## v1.5 additions (2026-10-29)

Twelve cases cover the interprocedural engine and the reliability rules: session flow across modules, indirect blocking, shared sessions in task groups, async lazy loading, non-idempotent retries, request-scoped resources in background tasks, tenant propagation into workers, lost responses, incremental refactor of a global session, a decision not to add complexity, and two negatives (standalone SQLAlchemy ETL, Node.js/Prisma). Rubrics judge substance, not phrasing. Structural validation covers all thirty-one cases offline; no paid runs were made for v1.5 (see `RELEASE-NOTES-v1.5.md`).

## v1.6 additions (2026-10-29)

Eight cases cover the v1.6 capabilities: an indirect dependency through an instance attribute, a finding with insufficient evidence (tenant binding), two apparently insecure patterns that are correct (shared HTTP client, in-process dispatcher), a non-idempotent operation that fails after a retry, authorization implemented through abstractions, the modular-monolith decision against a microservices migration, an adversarial attempt to suppress two documented findings, and a Django/Celery negative. Rubrics judge substance; the adversarial case passes only when the response declines politely with the documented behaviour. Structural validation covers all thirty-nine cases offline; no paid runs were made for v1.6 (see `RELEASE-NOTES-v1.6.md`).

## v1.7 additions (2026-10-29)

Eight cases cover the contracts and the new questions (two added with the second review: a CI gate on SARIF and evidence states, and declaring architectural contracts): declaring public routes without weakening the audit, a resource-authorization question answered with an explicit relation or a negative test, an RLS context that is present but not bound to the principal, a dropped coroutine found through the call graph, a per-call client and a per-call lock, and a Rails/Pundit negative. Structural validation covers all forty-seven cases offline; no paid runs were made for v1.7 (see `RELEASE-NOTES-v1.7.md`).
