# Compatibility fixtures: artefacts written by earlier versions

Frozen artefacts that earlier versions of the plugin wrote (v2.0.0, and since v2.2 also v2.1.0), kept verbatim so that every later version proves it still
reads them and treats them as documented. `tests/test_compat_artifacts.py` is the consumer. Do not regenerate these
files when the engines change: that is the point of them. Add a new directory when a schema is bumped.

| Fixture | Written by | Provenance |
|---|---|---|
| `generation-1.0/inventory-service/` | manifest `fastapi-architect/generation/1.0` (v2.0.0 increment 1) | `git archive 6cb86e9 plugins/fastapi-architect/examples/generated/inventory-service` |
| `generation-1.0/inventory-service.spec.json` | the `fastapi-architect/spec/1.0` contract that project was generated from | `git show 6cb86e9:plugins/fastapi-architect/examples/specs/inventory-service.json` (its digest is the one the manifest records) |
| `generation-1.1/inventory-service/` | manifest `fastapi-architect/generation/1.1` (v2.0.0 increment 4) | `git archive df10911 plugins/fastapi-architect/examples/generated/inventory-service` |
| `evolution-1.0.json`, `evolution-1.1.json` | evolution contracts `fastapi-architect/evolution/1.0` and `1.1` | frozen copies of `examples/evolutions/inventory-add-suppliers.json` and `inventory-add-product-fields.json`, pointing at `generation-1.1/inventory-service` |
| `operation-1.0/project/` | an operation journal `fastapi-architect/operation/1.0` interrupted during publication | produced by `journal.prepare` + `journal.publish` with a fault injected after the first file landed: `a.txt` is published, `b.txt` is pending, the staging and the backup are under `.fastapi-architect/ops/` |
| `adoption-1.0.json` | an adoption baseline `fastapi-architect/adoption/1.0` | produced by `evolve_fastapi.py adopt` on `tests/fixtures/adoption20/handmade_shop` with `adopt-items.json` |
| `audit-report-1.7.json` | an audit report, schema 1.7, with its fingerprints | `audit_fastapi.py tests/fixtures/sa_sessions --json` at v2.0.0 (`project_root` replaced by the repository-relative path) |

| `generation-1.3/parcels/` | manifest `fastapi-architect/generation/1.3` written by v2.1.0 (tool 2.1.0) | `git archive 7aff2b1 plugins/fastapi-architect/examples/generated/parcels` (the fifth example, before its phased evolutions) |
| `evolution-1.2-expand.json`, `evolution-1.2-contract.json` | evolution contracts `fastapi-architect/evolution/1.2` (phased: `backfill`, `add_indexes`, `contract_fields`, `authorization.contract`) | frozen copies of `examples/evolutions/parcels-expand-carrier.json` and `parcels-contract-carrier.json` at v2.1.0, pointing at `generation-1.3/parcels` and at the evidence below |
| `migration-evidence-1.0.json` | a migration evidence document `fastapi-architect/migration-evidence/1.0` written by the generated runner of v2.1.0 | frozen copy of `examples/evolutions/parcels-migration-evidence.sample.json` (dated 2026-10-01: stale by design) |
| `evolution-report-1.3.json` | an evolution report `evolution_report/1.3` (plan only, `--no-verify`) of the expand contract against the frozen 1.3 project | `evolve_fastapi.py plan --no-verify --format json` at v2.1.0; the absolute project and contract paths replaced by `<project>` and `<contract>` |

Manifests 1.2, evolution reports 1.1 and the current guarantees are covered by the committed examples under
`plugins/fastapi-architect/examples/` and by `tests/fixtures/design20/guarantees/`.
