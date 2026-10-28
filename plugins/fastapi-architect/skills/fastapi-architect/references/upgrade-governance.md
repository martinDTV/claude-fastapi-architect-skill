# Dependency upgrades, compatibility and engineering governance

## Trigger
FastAPI/Starlette/Pydantic/SQLAlchemy/Python upgrades, vulnerability advisories, tech debt, release hardening or incident follow-up.

## Versioned compatibility policy
- Discover currently installed and supported versions from repository lockfile and official upstream changelogs; do not assume latest versions from memory.
- Evaluate framework stack together: Python, FastAPI, Starlette, Pydantic, SQLAlchemy, database drivers, Uvicorn, OpenTelemetry packages and dependency extras.
- Treat a security update as risk management: impact, exploitability, rollback and verification. Avoid indiscriminate upgrades when a focused patch is possible.
- Lock dependencies, build immutable artifacts and provide SBOM/dependency audit evidence. External vulnerabilities need triage by reachability, not only numeric severity.

## Migration protocol
1. Record baseline tests, typechecking, OpenAPI snapshot, performance and dependency graph.
2. Enumerate removed or behavior-changing APIs and runtime warnings.
3. Upgrade one compatible group at a time. Keep downgrade/rollback strategy for runtime changes.
4. Run automated tests, schema diffs, integration with real PostgreSQL and ephemeral provider doubles.
5. Canary or staged rollout; observe error rate, latency, connection use and business outcomes.
6. Document decision and sunset old compatibility shims after consumers migrate.

## Review of generated code
Require location, evidence, impact, proposed fix, alternative and tests for each finding. Prefer minimal patches and no unrelated refactors. Explicitly state which commands ran; never fabricate audit pass or production behavior.

## ADR template
Context, forces, options, trade-off matrix, decision, expected failure modes, rollback, verification strategy, owner, expiration/review trigger. Review decisions when assumptions change, not on arbitrary schedules.

## Coverage ledger
For each new reference: trigger examples, negative decision example, at least one counterexample, test scenario, source/verification date, explicit dependencies. Keep main SKILL.md brief by linking to detailed reference only when invoked.
