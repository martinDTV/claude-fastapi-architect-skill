# FastAPI Architect v2 — extension pack (non-destructive)

This package adds eight specialized references and a scenario evaluation bank to the existing `fastapi-architect` plugin; it does not overwrite existing guidance.

## Installation

In a checkout of `martinDTV/claude-fastapi-architect-skill`:

```bash
python /path/to/fastapi-architect-v2-extension/scripts/install.py /path/to/claude-fastapi-architect-skill
```

The installer copies references into `plugins/fastapi-architect/skills/fastapi-architect/references/`, adds the reference routing section to `SKILL.md` once, and adds `evals/scenarios-v2.json`. It refuses to overwrite differently named existing files.

## Quality gates

```bash
python -m json.tool evals/scenarios-v2.json >/dev/null
# On a machine with Claude Code installed:
claude plugin validate .
```

Recommended: run existing evals as baseline, then run scenarios-v2, manually score architecture fit, correctness, security, minimalism, and verifiability. Validate example code and integrations against versions actually installed in target projects.

## New references

- decision-router.md — architecture selection and tradeoffs
- api-contracts.md — HTTP contracts, pagination, idempotency, optimistic concurrency
- authz-multitenancy.md — identity, authorization, RLS, token verification
- async-concurrency.md — cancellation, structured concurrency, transaction races
- distributed-reliability.md — outbox/inbox, durable queues, sagas
- database-evolution.md — constraints, migrations, backups, restore
- testing-matrix.md — adversarial cases, test layers, skill evaluation
- operations-production.md — SLO, incident response, rollout safety

This is a **first substantial extension**, not a claim to exhaust every FastAPI scenario. Its recommendations are conditional; repository-specific ADRs and verified constraints take precedence.
