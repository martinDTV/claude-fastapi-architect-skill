# Evidence-based test strategy

## Risk-driven pyramid
- Pure unit tests: invariants, authorization decisions, calculations, state machines.
- Integration: real PostgreSQL for transactions, RLS, unique constraints, migrations, concurrency; provider fakes for deterministic contracts.
- Contract: OpenAPI schema snapshot and compatibility, webhook event variants, producer/consumer compatibility.
- E2E: a few real user workflows in controlled environments, including auth and tenant switch.
- Nonfunctional: k6/Locust load with SLA, dependency downtime, idempotency races, security scans, migration/restore drills.

## Mandatory adversarial examples
1. Two concurrent writes with the same ETag; exactly one succeeds.
2. Cross-tenant RLS query and cross-tenant foreign-key attempt fail closed.
3. Expired/revoked/foreign-client JWT rejected; JWKS rotation and removed key tested.
4. Duplicate/late/reordered webhook cannot duplicate side effects.
5. Worker crash after DB commit can resume without duplicating delivery.
6. List endpoint sort injection and unbounded pagination rejected.
7. Secret and patient data absent from logs, errors, headers, traces.
8. Time-zone DST fold/gap behavior deterministic.
9. New schema with old code and old schema with new code reviewed for rollout safety.
10. Third-party outage does not exhaust API worker pools.

## Evaluation of the skill itself
For each scenario score: architecture right-sizing (0-3), correctness of contracts (0-3), security (0-3), implementability/verification (0-3), avoids unnecessary complexity (0-3). Penalize unsupported claims and invented APIs. Compare baseline against enhanced skill. Keep reproducible fixtures and model outputs; evaluate both task-selection and implementation quality.

## Tooling checks
Format, lint, strict typecheck, import boundaries, pytest, migrations, dependency audit and tested sample snippets. Mark checks not run; a green suite is never proof of security by itself.
