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
for script in (auditor,shim):compile(script.read_text(encoding='utf-8'),str(script),'exec')
assert re.search(r'TOOL_VERSION\s*=\s*"'+re.escape(version)+'"',auditor.read_text(encoding='utf-8')), f'Auditor TOOL_VERSION != {version}'
assert 'scripts/audit_fastapi.py' in skill, 'SKILL.md does not reference scripts/audit_fastapi.py'
assert (root/'tests/test_audit_fastapi.py').is_file(), 'Missing tests/test_audit_fastapi.py'
major_minor='.'.join(version.split('.')[:2])
assert (root/f'RELEASE-NOTES-v{major_minor}.md').is_file(), f'Missing RELEASE-NOTES-v{major_minor}.md'
assert f'v{major_minor}' in (root/'README.md').read_text(encoding='utf-8'), f'README does not mention v{major_minor}'
print(f'OK: {len(refs)} references, all linked, manifest version {version}, eval JSON valid, auditor + entry point + tests + release notes present')
