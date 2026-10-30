# Vendored schemas (test data)

| File | Source | Version | SHA-256 (first 16 hex) | Bytes |
|---|---|---|---|---|
| `sarif-schema-2.1.0.json` | https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json | SARIF 2.1.0 errata01 (OASIS Standard), downloaded 2026-10-30 | `c3b4bb2d60938974` | 112768 |

Used by `tests/test_sarif_schema.py` and `scripts/validate_sarif.py` to validate the auditor's SARIF output with
`jsonschema` (Draft 4 semantics, as the schema declares). The schema is not a runtime dependency of the auditor;
the validation step is skipped when `jsonschema` is not installed.
