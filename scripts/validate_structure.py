#!/usr/bin/env python3
"""Validate repository packaging and reference links. No network or Claude required."""
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
print(f'OK: {len(refs)} references, all linked, manifest version {versions.pop()}, eval JSON valid')
