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

Eight cases cover the v1.4 auditor categories: real risks on a fixture (`detect-session-risks`), a false positive to reject, an existing architecture to defend, a concurrency test to propose, a justified destructive migration, security that cannot be verified with AST alone, selection of the database-evolution guidance, and a Django negative. Structural validation (`tests/test_plugin_evals.py`) covers all nineteen cases without cost. Paid runs for v1.4 were limited to a small regression sample; see `RELEASE-NOTES-v1.4.md` for what ran and what it cost.
