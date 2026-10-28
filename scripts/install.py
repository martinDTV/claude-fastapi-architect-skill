#!/usr/bin/env python3
"""Non-destructive extension installer for a local checkout of fastapi-architect."""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
ROUTING = '''\n\n## Extended decision and implementation references (v2)\n\nLoad only the relevant reference for each task; preserve the existing architecture-stage selection above.\n\n| Reference | Trigger |\n|---|---|\n| `references/decision-router.md` | Choosing architecture, reviewing a system, evaluating CQRS/DDD/microservices |\n| `references/api-contracts.md` | API contracts, pagination, sort, ETag, idempotency, compatibility |\n| `references/authz-multitenancy.md` | Authentication, authorization, OIDC/JWKS, RLS, tenant isolation, security |\n| `references/async-concurrency.md` | Async, cancellation, timeouts, lost updates, concurrency, load |\n| `references/distributed-reliability.md` | Webhooks, outbox/inbox, workers, microservices, sagas, retries |\n| `references/database-evolution.md` | PostgreSQL, migrations, indexing, backups, recovery |\n| `references/testing-matrix.md` | Unit/integration/e2e/contract, adversarial tests, skill evaluation |\n| `references/operations-production.md` | CI/CD, SLO, observability, capacity, incidents, cloud deployment |\n\nDo not load all references automatically. Preserve existing repo conventions and project ADRs. Treat defaults as heuristics, not universally binding rules. State what has actually been verified; do not claim a test ran without running it.\n'''

def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python scripts/install.py /path/to/claude-fastapi-architect-skill')
    repo = Path(sys.argv[1]).expanduser().resolve()
    target = repo / 'plugins/fastapi-architect/skills/fastapi-architect'
    skill = target / 'SKILL.md'
    if not skill.is_file() or not (repo / '.claude-plugin/marketplace.json').is_file():
        raise SystemExit('Target must be an existing checkout with skill and marketplace manifest')
    for src in sorted((ROOT / 'references').glob('*.md')):
        dest = target / 'references' / src.name
        if dest.exists() and dest.read_bytes() != src.read_bytes():
            raise SystemExit(f'Refusing overwrite: {dest}')
    ev = repo / 'evals/scenarios-v2.json'
    original = ROOT / 'evals/scenarios.json'
    if ev.exists() and ev.read_bytes() != original.read_bytes():
        raise SystemExit(f'Refusing overwrite: {ev}')
    for src in sorted((ROOT / 'references').glob('*.md')):
        shutil.copyfile(src, target / 'references' / src.name)
    shutil.copyfile(original, ev)
    content = skill.read_text()
    if '## Extended decision and implementation references (v2)' not in content:
        skill.write_text(content + ROUTING)
    print('Installed v2 extension. Review git diff; run plugin validation and evals before committing.')

if __name__ == '__main__':
    main()
