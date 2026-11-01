#!/usr/bin/env python3
"""Validate repository packaging and reference links. No network or Claude required.

Checks: reference links from SKILL.md, manifest version consistency, eval JSON
validity, and (since v1.3) the static auditor, its root entry point, its test
suite and the release notes for the current version.
"""
import json
import re
from pathlib import Path

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
for p in (root/'evals').glob('*.json'):
    json.loads(p.read_text(encoding='utf-8'))
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
for script in (auditor,shim,*sorted(package.rglob('*.py'))):
    compile(script.read_text(encoding='utf-8'),str(script),'exec')
assert re.search(r'TOOL_VERSION\s*=\s*"'+re.escape(version)+'"',(base/'scripts/fastapi_common/version.py').read_text(encoding='utf-8')), f'fastapi_common TOOL_VERSION != {version}'
assert 'from fastapi_common.version import TOOL_VERSION' in (package/'model.py').read_text(encoding='utf-8'), 'the auditor must re-export the single version constant'
assert 'scripts/audit_fastapi.py' in skill, 'SKILL.md does not reference scripts/audit_fastapi.py'
for t in ('test_audit_fastapi.py','test_audit_sqlalchemy.py','test_audit_transactions.py','test_audit_migrations.py','test_audit_security.py','test_audit_architecture.py','test_audit_v14_cli.py','test_audit_interprocedural.py','test_audit_v16.py','test_audit_v17.py','test_audit_v18.py','test_audit_v19.py','test_sarif_schema.py','test_plugin_evals.py'):
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
# v1.9: verified remediation package, tool (skill + root shim) and fixture
for mod in ('__init__.py','model.py','patches.py','isolation.py','pipeline.py','render.py'):
    assert (package/'remediate'/mod).is_file(), f'Missing fastapi_audit/remediate/{mod}'
rem_tool=base/'scripts/audit_remediate.py'
rem_shim=root/'scripts/audit_remediate.py'
assert rem_tool.is_file(), f'Missing remediation tool: {rem_tool}'
assert rem_shim.is_file() and 'audit_remediate.py' in rem_shim.read_text(encoding='utf-8'), 'Root audit_remediate.py does not forward to the skill tool'
compile(rem_tool.read_text(encoding='utf-8'),str(rem_tool),'exec')
assert (root/'tests/fixtures/remediation19/tests/test_defects.py').is_file(), 'Missing remediation19 fixture'
assert (base/'references/safe-refactoring.md').is_file(), 'Missing references/safe-refactoring.md'
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
# v2.0: design engine (contract, advisor, blueprint, generator, verification, report), tool (skill + root shim), examples, fixtures, docs
design=base/'scripts/fastapi_design'
for mod in ('__init__.py','spec.py','advisor.py','blueprint/__init__.py','blueprint/_base.py','blueprint/_helpers.py','blueprint/scaffold.py','blueprint/security.py','blueprint/persistence.py','blueprint/modules.py','blueprint/testing.py','blueprint/security_tests.py','generator.py','verify.py','report.py','pipeline.py','discovery.py','evolution.py','journal.py','transforms.py','adoption.py'):
    assert (design/mod).is_file(), f'Missing fastapi_design/{mod}'
    compile((design/mod).read_text(encoding='utf-8'),str(design/mod),'exec')
design_tool=base/'scripts/design_fastapi.py'
design_shim=root/'scripts/design_fastapi.py'
assert design_tool.is_file(), f'Missing design tool: {design_tool}'
assert design_shim.is_file() and 'design_fastapi.py' in design_shim.read_text(encoding='utf-8'), 'Root design_fastapi.py does not forward to the skill tool'
compile(design_tool.read_text(encoding='utf-8'),str(design_tool),'exec')
for spec_name in ('inventory-service.json','orders-platform.json','team-notes.json','tenant-projects.json'):
    spec_doc=json.loads((base.parent.parent/'examples/specs'/spec_name).read_text(encoding='utf-8'))
    assert spec_doc.get('schema') in ('fastapi-architect/spec/1.0','fastapi-architect/spec/1.1','fastapi-architect/spec/1.2'), f'examples/specs/{spec_name}: unexpected schema'
assert json.loads((base.parent.parent/'examples/specs/tenant-projects.json').read_text(encoding='utf-8'))['security']['multi_tenant']['strategy']=='shared_schema_rls', 'tenant-projects must be the row-level-security example'
assert (base.parent.parent/'examples/generated/inventory-service/.fastapi-architect/generation.json').is_file(), 'Missing generated example manifest'
for snapshot in ('inventory-service','orders-platform','team-notes','tenant-projects','parcels'):
    assert (base.parent.parent/f'examples/generated/{snapshot}/.fastapi-architect/generation.json').is_file(), f'Missing generated example manifest: {snapshot}'
assert (base.parent.parent/'examples/generated/tenant-projects/tests/test_rls.py').is_file() and (base.parent.parent/'examples/generated/team-notes/tests/test_auth.py').is_file() and (base.parent.parent/'examples/generated/team-notes/src/team_notes/core/authz.py').is_file(), 'Missing the security artefacts of the increment 5 examples'
assert 'FORCE ROW LEVEL SECURITY' in next((base.parent.parent/'examples/generated/tenant-projects/migrations/versions').glob('*_initial_schema.py')).read_text(encoding='utf-8'), 'tenant-projects migration does not force row-level security'
assert (base.parent.parent/'examples/generated/orders-platform/alembic.ini').is_file() and list((base.parent.parent/'examples/generated/orders-platform/migrations/versions').glob('*_initial_schema.py')), 'Missing Alembic environment in the PostgreSQL example'
assert (base.parent.parent/'examples/generated/README.md').is_file() and (base.parent.parent/'examples/generated/VERIFICATION.md').is_file(), 'Missing README/VERIFICATION of the generated examples'
assert (root/'tests/fixtures/regressions_v20/route_composition/.audit-fastapi.json').is_file(), 'Missing v2.0 route-composition regression fixture'
assert (root/'tests/test_design_v20_postgresql.py').is_file() and (root/'tests/_pg_support.py').is_file(), 'Missing the opt-in PostgreSQL verification of generated backends'
assert (root/'docs/guides/generated-postgresql-projects.md').is_file(), 'Missing docs/guides/generated-postgresql-projects.md'
evolve_tool=base/'scripts/evolve_fastapi.py'
evolve_shim=root/'scripts/evolve_fastapi.py'
assert evolve_tool.is_file(), f'Missing evolution tool: {evolve_tool}'
assert evolve_shim.is_file() and 'evolve_fastapi.py' in evolve_shim.read_text(encoding='utf-8'), 'Root evolve_fastapi.py does not forward to the skill tool'
compile(evolve_tool.read_text(encoding='utf-8'),str(evolve_tool),'exec')
for contract_name in ('inventory-add-suppliers.json','orders-add-shipments.json','inventory-add-product-fields.json','orders-add-customer-columns.json','tenant-projects-add-milestones.json'):
    contract_doc=json.loads((base.parent.parent/'examples/evolutions'/contract_name).read_text(encoding='utf-8'))
    assert contract_doc.get('schema') in ('fastapi-architect/evolution/1.0','fastapi-architect/evolution/1.1'), f'examples/evolutions/{contract_name}: unexpected schema'
assert (root/'tests/test_evolve_v20.py').is_file(), 'Missing tests/test_evolve_v20.py'
for test_mod in ('tests/test_evolve_v20_recovery.py','tests/test_evolve_v20_adoption.py'):
    assert (root/test_mod).is_file(), f'Missing {test_mod}'
assert (root/'tests/fixtures/adoption20/handmade_shop/app/routers/items.py').is_file() and (root/'tests/fixtures/adoption20/handmade_shop/tests/test_items.py').is_file(), 'Missing the hand-written adoption fixture'
assert json.loads((root/'tests/fixtures/adoption20/adopt-items.json').read_text(encoding='utf-8')).get('schema')=='fastapi-architect/adoption/1.0', 'Missing or wrong adoption contract fixture'
assert (base/'references/evolution-contracts.md').is_file(), 'Missing references/evolution-contracts.md'
assert (root/'docs/guides/evolving-generated-projects.md').is_file(), 'Missing docs/guides/evolving-generated-projects.md'
assert 'guides/evolving-generated-projects.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the evolution guide'
for snapshot in ('inventory-service','orders-platform','team-notes','tenant-projects','parcels'):
    manifest_doc=json.loads((base.parent.parent/f'examples/generated/{snapshot}/.fastapi-architect/generation.json').read_text(encoding='utf-8'))
    assert manifest_doc.get('schema')=='fastapi-architect/generation/1.3' and 'spec_document' in manifest_doc and 'security' in manifest_doc and 'phased_changes' in manifest_doc, f'{snapshot}: manifest must be 1.3 with the embedded contract, the security record and the phased changes'
assert 'guides/generated-postgresql-projects.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the PostgreSQL guide'
assert (root/'tests/fixtures/design20/specs/valid/minimal.json').is_file() and (root/'tests/fixtures/design20/specs/invalid/dependency_cycle.json').is_file(), 'Missing design20 spec fixtures'
assert (root/'tests/fixtures/regressions_v20/nested_routers/src/shop/api/router.py').is_file(), 'Missing v2.0 nested-routers regression fixture'
assert (root/'tests/test_design_v20.py').is_file() and (root/'tests/test_audit_v20.py').is_file() and (root/'tests/test_design_v20_security.py').is_file(), 'Missing v2.0 test modules'
assert (root/'tests/fixtures/design20/specs/valid/rls_claim_binding.json').is_file() and (root/'tests/fixtures/design20/specs/invalid/security12_policy.json').is_file(), 'Missing schema 1.2 security fixtures'
assert (root/'tests/test_audit_v20_named_authz.py').is_file() and (root/'tests/fixtures/regressions_v20/named_authz/named/security.py').is_file() and (root/'tests/fixtures/regressions_v20/named_authz_privilege/priv/api.py').is_file(), 'Missing the name-based authorization negative controls (security hardening)'
assert (root/'tests/test_acceptance_v20.py').is_file(), 'Missing the v2.0.0 end-to-end acceptance scenario'
assert (root/'docs/releases/ACCEPTANCE-v2.0.0.md').is_file() and 'ACCEPTANCE-v2.0.0.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'Missing or unindexed acceptance report docs/releases/ACCEPTANCE-v2.0.0.md'
for name in ('inventory-service','orders-platform','team-notes','tenant-projects','oidc_owner_scoped','rls_claim_binding','tenant_unbound'):
    assert 'not_claimed' in json.loads((root/f'tests/fixtures/design20/guarantees/{name}.json').read_text(encoding='utf-8')), f'Missing or incomplete guarantees fixture {name}'
assert (base.parent.parent/'examples/generated/team-notes/tests/test_jwks.py').is_file(), 'team-notes lacks the generated HTTPS JWKS tests'
assert 'authorization_named_only' in (base/'references/audit-engine.md').read_text(encoding='utf-8'), 'references/audit-engine.md does not document authorization_named_only'
assert (root/'docs/guides/identity-authorization-and-rls.md').is_file(), 'Missing docs/guides/identity-authorization-and-rls.md'
assert 'guides/identity-authorization-and-rls.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the identity guide'
assert '## 8. Identity, authorization and tenant isolation' in (base/'references/design-contracts.md').read_text(encoding='utf-8'), 'references/design-contracts.md lacks the security section'
assert (base/'references/design-contracts.md').is_file(), 'Missing references/design-contracts.md'
assert (root/'docs/architecture/design-engine-v2.md').is_file(), 'Missing docs/architecture/design-engine-v2.md'
assert 'architecture/design-engine-v2.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the architecture record'
# v2.1 increment 0: shared infrastructure, blueprint package, lint configuration, durable journal, compatibility fixtures
common=base/'scripts/fastapi_common'
for mod in ('__init__.py','artefacts.py','fs.py','version.py'):
    assert (common/mod).is_file(), f'Missing fastapi_common/{mod}'
    compile((common/mod).read_text(encoding='utf-8'),str(common/mod),'exec')
assert not (design/'blueprint.py').exists(), 'blueprint.py must not coexist with the blueprint package'
ruff_cfg=(root/'ruff.toml')
assert ruff_cfg.is_file() and 'target-version = "py39"' in ruff_cfg.read_text(encoding='utf-8'), 'Missing ruff.toml with the Python 3.9 target'
gitignore=(root/'.gitignore').read_text(encoding='utf-8').splitlines()
for name in ('.ruff_cache/','.pytest_cache/','.mypy_cache/','__pycache__/','.DS_Store'):
    assert name in gitignore, f'.gitignore does not list {name}'
for rel in ('tests/fixtures/compat/README.md','tests/fixtures/compat/generation-1.0/inventory-service/.fastapi-architect/generation.json','tests/fixtures/compat/generation-1.0/inventory-service.spec.json','tests/fixtures/compat/generation-1.1/inventory-service/.fastapi-architect/generation.json','tests/fixtures/compat/evolution-1.0.json','tests/fixtures/compat/evolution-1.1.json','tests/fixtures/compat/operation-1.0/project/a.txt','tests/fixtures/compat/adoption-1.0.json','tests/fixtures/compat/audit-report-1.7.json','tests/test_foundations_v21.py','tests/test_compat_artifacts.py','docs/architecture/discovery-v2.1.md','docs/releases/RELEASE-NOTES-v2.1.md'):
    assert (root/rel).is_file(), f'Missing {rel}'
assert 'architecture/discovery-v2.1.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the v2.1 discovery record'
assert 'releases/RELEASE-NOTES-v2.1.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the v2.1 release notes'
# v2.1 increment 1: the container executor, its in-container scripts, the shared CLI surface, the fixture, the tests and the documents
for mod in ('containers.py','container_cli.py','_sandbox_probe.py','_prepare_env.py'):
    assert (package/'remediate'/mod).is_file(), f'Missing fastapi_audit/remediate/{mod}'
    compile((package/'remediate'/mod).read_text(encoding='utf-8'),str(package/'remediate'/mod),'exec')
for rel in ('tests/test_container_v21.py','tests/fixtures/container21/sandbox/tests/test_controls.py','docs/guides/isolated-execution-with-containers.md','docs/architecture/adr-2.1-03-isolated-container-executor.md'):
    assert (root/rel).is_file(), f'Missing {rel}'
for rel in ('guides/isolated-execution-with-containers.md','architecture/adr-2.1-03-isolated-container-executor.md'):
    assert rel in (root/'docs/README.md').read_text(encoding='utf-8'), f'docs/README.md does not index {rel}'
assert 'Isolated executor: containers' in (base/'references/safe-refactoring.md').read_text(encoding='utf-8'), 'references/safe-refactoring.md does not document the container executor'
assert '--executor container' in skill, 'SKILL.md does not mention the container executor'
# v2.1 increment 2: phased migrations (expand, backfill, verify, contract), the fifth example, the tests and the documents
for rel in ('scripts/fastapi_design/blueprint/phased.py','scripts/fastapi_design/migrations.py'):
    assert (base/rel).is_file(), f'Missing {rel}'
    compile((base/rel).read_text(encoding='utf-8'),str(base/rel),'exec')
assert json.loads((base.parent.parent/'examples/specs/parcels.json').read_text(encoding='utf-8')).get('schema')=='fastapi-architect/spec/1.2', 'examples/specs/parcels.json: unexpected schema'
for contract_name in ('parcels-expand-carrier.json','parcels-contract-carrier.json'):
    assert json.loads((base.parent.parent/'examples/evolutions'/contract_name).read_text(encoding='utf-8')).get('schema')=='fastapi-architect/evolution/1.2', f'examples/evolutions/{contract_name}: must be an evolution 1.2 contract'
assert json.loads((base.parent.parent/'examples/evolutions/parcels-migration-evidence.sample.json').read_text(encoding='utf-8')).get('schema')=='fastapi-architect/migration-evidence/1.0', 'the sample migration evidence must be migration-evidence/1.0'
assert not (base.parent.parent/'examples/generated/parcels/src/parcels/ops').exists(), 'the parcels snapshot is the baseline before the expand evolution: no ops package'
for rel in ('tests/test_migrations_v21.py','tests/test_migrations_v21_postgresql.py','docs/guides/phased-migrations-and-backfills.md','docs/architecture/adr-2.1-01-phased-migrations.md'):
    assert (root/rel).is_file(), f'Missing {rel}'
for rel in ('guides/phased-migrations-and-backfills.md','architecture/adr-2.1-01-phased-migrations.md'):
    assert rel in (root/'docs/README.md').read_text(encoding='utf-8'), f'docs/README.md does not index {rel}'
assert 'fastapi-architect/evolution/1.2' in (base/'references/evolution-contracts.md').read_text(encoding='utf-8') and 'migration-evidence/1.0' in (base/'references/evolution-contracts.md').read_text(encoding='utf-8'), 'references/evolution-contracts.md does not document contract 1.2 and the migration evidence'
assert 'phased-migrations-and-backfills.md' in skill, 'SKILL.md does not mention the phased migrations guide'
assert 'parcels' in (base.parent.parent/'examples/generated/README.md').read_text(encoding='utf-8'), 'examples/generated/README.md does not list the parcels example'
# v2.1 increment 6: the acceptance of the release candidate
assert (root/'tests/test_acceptance_v21.py').is_file(), 'Missing the v2.1.0 acceptance module'
assert (root/'docs/releases/ACCEPTANCE-v2.1.0.md').is_file() and 'ACCEPTANCE-v2.1.0.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'Missing or unindexed acceptance report docs/releases/ACCEPTANCE-v2.1.0.md'
assert 'Extension v2.1' in (root/'README.md').read_text(encoding='utf-8'), 'README does not describe v2.1'
# v2.2 increment 0: span-exact edits, the pure three-way merge, the project lock, journal 1.1, the adversarial corpus and the v2.1.0 compatibility fixtures
for rel in ('scripts/fastapi_design/spans.py','scripts/fastapi_design/merge.py','scripts/fastapi_design/locking.py'):
    assert (base/rel).is_file(), f'Missing {rel}'
    compile((base/rel).read_text(encoding='utf-8'),str(base/rel),'exec')
for rel in ('tests/test_spans_v22.py','tests/test_merge_v22.py','tests/test_journal_v22.py','tests/fixtures/transforms22/app_factory.py','tests/fixtures/compat/generation-1.3/parcels/.fastapi-architect/generation.json','tests/fixtures/compat/evolution-1.2-expand.json','tests/fixtures/compat/evolution-1.2-contract.json','tests/fixtures/compat/migration-evidence-1.0.json','tests/fixtures/compat/evolution-report-1.3.json','docs/architecture/discovery-v2.2.md','docs/releases/RELEASE-NOTES-v2.2.md'):
    assert (root/rel).is_file(), f'Missing {rel}'
for rel in ('architecture/discovery-v2.2.md','releases/RELEASE-NOTES-v2.2.md'):
    assert rel in (root/'docs/README.md').read_text(encoding='utf-8'), f'docs/README.md does not index {rel}'
assert 'operation/1.1' in (base/'scripts/fastapi_design/journal.py').read_text(encoding='utf-8') and 'ops list' in skill, 'the journal 1.1 and the ops subcommands are not documented'
major_minor='.'.join(version.split('.')[:2])
assert (root/f'docs/releases/RELEASE-NOTES-v{major_minor}.md').is_file(), f'Missing docs/releases/RELEASE-NOTES-v{major_minor}.md'
assert (root/'docs/README.md').is_file(), 'Missing docs/README.md (documentation index)'
assert not list(root.glob('RELEASE-NOTES-*.md')) and not (root/'MERGE-NOTES.md').exists(), 'Release notes belong in docs/releases/, not in the repository root'
assert f'releases/RELEASE-NOTES-v{major_minor}.md' in (root/'docs/README.md').read_text(encoding='utf-8'), 'docs/README.md does not index the current release notes'
assert f'v{major_minor}' in (root/'README.md').read_text(encoding='utf-8'), f'README does not mention v{major_minor}'
print(f'OK: {len(refs)} references, all linked, manifest version {version}, eval JSON valid, auditor + entry point + tests + release notes present, {len(cases)} plugin eval cases, reference implementation present')
