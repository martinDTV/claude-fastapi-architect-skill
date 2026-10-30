#!/usr/bin/env python3
"""Validate repository packaging and reference links. No network or Claude required.

Checks: reference links from SKILL.md, manifest version consistency, eval JSON
validity, and (since v1.3) the static auditor, its root entry point, its test
suite and the release notes for the current version.
"""
from pathlib import Path
import json,re,sys
root=Path(__file__).resolve().parents[1]
base=root/'plugins/fastapi-architect/skills/fastapi-architect'
skill=(base/'SKILL.md').read_text(encoding='utf-8')
refs={p.name for p in (base/'references').glob('*.md')}
links=set(re.findall(r'references/([a-z0-9-]+\.md)',skill))
missing=links-refs
unlinked=refs-links
market=json.loads((root/'.claude-plugin/marketplace.json').read_text())
plugin=json.loads((root/'plugins/fastapi-architect/.claude-plugin/plugin.json').read_text())
versions={market['metadata']['version'],market['plugins'][0]['version'],plugin['version']}
for p in (root/'evals').glob('*.json'):json.loads(p.read_text(encoding='utf-8'))
assert not missing, f'Missing references: {missing}'
assert not unlinked, f'Unlinked references: {unlinked}'
assert len(versions)==1, f'Version mismatch: {versions}'
assert plugin['name']=='fastapi-architect'
version=versions.pop()
# v1.3: static auditor shipped inside the skill, forwarded from the repo root, tested, documented.
auditor=base/'scripts/audit_fastapi.py'
shim=root/'scripts/audit_fastapi.py'
assert auditor.is_file(), f'Missing auditor: {auditor}'
assert shim.is_file(), f'Missing root entry point: {shim}'
assert 'skills' in shim.read_text(encoding='utf-8') and 'audit_fastapi.py' in shim.read_text(encoding='utf-8'), 'Root entry point does not forward to the skill auditor'
package=base/'scripts/fastapi_audit'
assert (package/'__init__.py').is_file(), f'Missing auditor package: {package}'
for script in (auditor,shim,*sorted(package.rglob('*.py'))):compile(script.read_text(encoding='utf-8'),str(script),'exec')
assert re.search(r'TOOL_VERSION\s*=\s*"'+re.escape(version)+'"',(package/'model.py').read_text(encoding='utf-8')), f'Auditor TOOL_VERSION != {version}'
assert 'scripts/audit_fastapi.py' in skill, 'SKILL.md does not reference scripts/audit_fastapi.py'
for t in ('test_audit_fastapi.py','test_audit_sqlalchemy.py','test_audit_transactions.py','test_audit_migrations.py','test_audit_security.py','test_audit_architecture.py','test_audit_v14_cli.py','test_audit_interprocedural.py','test_audit_v16.py','test_audit_v17.py','test_audit_v18.py','test_sarif_schema.py','test_plugin_evals.py'):
    assert (root/'tests'/t).is_file(), f'Missing tests/{t}'
evals_dir=root/'plugins/fastapi-architect/evals'
cases=[d for d in evals_dir.iterdir() if d.is_dir() and d.name!='results'] if evals_dir.is_dir() else []
assert (evals_dir/'README.md').is_file(), 'Missing plugins/fastapi-architect/evals/README.md'
assert len(cases)>=9, f'Expected at least 9 plugin eval cases, found {len(cases)}'
for d in cases:
    assert (d/'prompt.md').is_file() or (d/'case.yaml').is_file(), f'Eval case without prompt.md or case.yaml: {d.name}'
    assert (d/'graders').is_dir() or (d/'case.yaml').is_file(), f'Eval case without graders: {d.name}'
assert 'plugins/fastapi-architect/evals/results/' in (root/'.gitignore').read_text(encoding='utf-8'), 'evals results dir not git-ignored'
assert (root/'tests/test_plugin_evals.py').is_file(), 'Missing tests/test_plugin_evals.py'
# v1.6: ground-truth corpus and the precision/benchmark script
assert (root/'tests/fixtures/truth16/README.md').is_file(), 'Missing tests/fixtures/truth16/README.md'
for part in ('dev','acceptance','holdout'):
    assert (root/f'tests/fixtures/truth17/{part}/EXPECTATIONS.md').is_file(), f'Missing tests/fixtures/truth17/{part}/EXPECTATIONS.md'
assert (root/'scripts/audit_precision.py').is_file(), 'Missing scripts/audit_precision.py'
# v1.8: precision corpora, the report comparison tool (skill + root shim) and the remediation module
for part in ('dev','acceptance','holdout'):
    assert (root/f'tests/fixtures/truth18/{part}/EXPECTATIONS.md').is_file(), f'Missing tests/fixtures/truth18/{part}/EXPECTATIONS.md'
diff_tool=base/'scripts/audit_diff.py'
diff_shim=root/'scripts/audit_diff.py'
assert diff_tool.is_file(), f'Missing comparison tool: {diff_tool}'
assert diff_shim.is_file() and 'audit_diff.py' in diff_shim.read_text(encoding='utf-8'), 'Root audit_diff.py does not forward to the skill tool'
compile(diff_tool.read_text(encoding='utf-8'),str(diff_tool),'exec')
assert (package/'remediation.py').is_file(), 'Missing fastapi_audit/remediation.py'
compile((root/'scripts/audit_precision.py').read_text(encoding='utf-8'),'audit_precision.py','exec')
assert (root/'tests/fixtures/contracts_declared/.audit-fastapi.json').is_file(), 'Missing declared-contracts fixture'
assert (root/'tests/fixtures/regressions_v17/.audit-fastapi.json').is_file(), 'Missing v1.7 regression fixture'
assert (root/'tests/schemas/sarif-schema-2.1.0.json').is_file(), 'Missing vendored SARIF schema'
assert (root/'scripts/validate_sarif.py').is_file(), 'Missing scripts/validate_sarif.py'
compile((root/'scripts/validate_sarif.py').read_text(encoding='utf-8'),'validate_sarif.py','exec')
example=root/'plugins/fastapi-architect/examples/tenant-ledger'
for f in ('README.md','VERIFICATION.md','LIMITATIONS.md','pyproject.toml','uv.lock','alembic.ini','src/ledger/main.py','tests/conftest.py','tests/test_adversarial.py','tests/test_tenant_binding.py'):
    assert (example/f).is_file(), f'Missing reference implementation file: {f}'
assert 'verified' in (example/'VERIFICATION.md').read_text(encoding='utf-8'), 'VERIFICATION.md does not state the label'
major_minor='.'.join(version.split('.')[:2])
assert (root/f'RELEASE-NOTES-v{major_minor}.md').is_file(), f'Missing RELEASE-NOTES-v{major_minor}.md'
assert f'v{major_minor}' in (root/'README.md').read_text(encoding='utf-8'), f'README does not mention v{major_minor}'
print(f'OK: {len(refs)} references, all linked, manifest version {version}, eval JSON valid, auditor + entry point + tests + release notes present, {len(cases)} plugin eval cases, reference implementation present')
