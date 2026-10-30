#!/usr/bin/env python3
"""Validate a SARIF document produced by the auditor against the official SARIF 2.1.0 schema and the
auditor's own interoperability expectations.

Usage: validate_sarif.py REPORT.sarif [--json REPORT.json]

Schema: tests/schemas/sarif-schema-2.1.0.json (OASIS SARIF 2.1.0 errata01; see tests/schemas/README.md).
Needs the ``jsonschema`` package for the schema step (skipped with a notice when missing); the
structural checks below run with the standard library only. Exit status 0 when every check passes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO, "tests", "schemas", "sarif-schema-2.1.0.json")
LEVELS = {"error", "warning", "note", "none"}


def schema_errors(document: Dict[str, Any]) -> Optional[List[str]]:
    """Problems reported by the official schema, or None when jsonschema is not installed."""
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return None
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))]


def structural_errors(document: Dict[str, Any], report: Optional[Dict[str, Any]] = None) -> List[str]:
    problems: List[str] = []
    if document.get("version") != "2.1.0":
        problems.append("version must be 2.1.0")
    if "sarif-schema-2.1.0.json" not in str(document.get("$schema", "")):
        problems.append("$schema must point at the SARIF 2.1.0 schema")
    runs = document.get("runs") or []
    if len(runs) != 1:
        problems.append("exactly one run expected")
        return problems
    run = runs[0]
    rules = run.get("tool", {}).get("driver", {}).get("rules", [])
    ids = [r.get("id") for r in rules]
    if len(ids) != len(set(ids)):
        problems.append("duplicate rule ids")
    bases = run.get("originalUriBaseIds", {})
    if "%SRCROOT%" not in bases or not str(bases["%SRCROOT%"].get("uri", "")).startswith("file://"):
        problems.append("originalUriBaseIds.%SRCROOT% must be a file:// URI")
    fingerprints = set()
    for i, result in enumerate(run.get("results", [])):
        where = f"results[{i}]"
        if result.get("ruleId") not in ids:
            problems.append(f"{where}: ruleId not declared in tool.driver.rules")
        idx = result.get("ruleIndex")
        if not isinstance(idx, int) or idx >= len(rules) or rules[idx].get("id") != result.get("ruleId"):
            problems.append(f"{where}: ruleIndex does not point at its rule")
        if result.get("level") not in LEVELS:
            problems.append(f"{where}: invalid level {result.get('level')!r}")
        for loc in result.get("locations", []):
            art = loc.get("physicalLocation", {}).get("artifactLocation", {})
            uri = str(art.get("uri", ""))
            if uri.startswith("/") or "://" in uri or art.get("uriBaseId") != "%SRCROOT%":
                problems.append(f"{where}: artifact location must be relative to %SRCROOT% ({uri!r})")
            region = loc.get("physicalLocation", {}).get("region", {})
            if region.get("startLine", 0) < 1 or region.get("startColumn", 0) < 1:
                problems.append(f"{where}: region lines and columns are 1-based")
        fp = result.get("partialFingerprints", {}).get("fastapiArchitect/fingerprint/v1")
        if not fp:
            problems.append(f"{where}: missing partial fingerprint")
        fingerprints.add(fp)
        props = result.get("properties", {})
        if props.get("evidence") not in {"verified", "inferred", "unknown"}:
            problems.append(f"{where}: evidence must be verified/inferred/unknown")
        if props.get("evidence_state") not in {"static_confirmed", "inferred", "insufficient", "partial", "suppressed", "reproduced"}:
            problems.append(f"{where}: invalid evidence_state")
        for sup in result.get("suppressions", []):
            if sup.get("kind") != "inSource":
                problems.append(f"{where}: suppression kind must be inSource")
        if "baselineState" in result and result["baselineState"] not in {"new", "unchanged", "updated", "absent"}:
            problems.append(f"{where}: invalid baselineState")
    if report is not None:
        findings = report.get("findings", [])
        if len(findings) != len(run.get("results", [])):
            problems.append(f"results ({len(run.get('results', []))}) and JSON findings ({len(findings)}) differ")
        json_fps = {f.get("fingerprint") for f in findings}
        if json_fps != fingerprints:
            problems.append("fingerprints differ between SARIF and the JSON report")
        suppressed_json = sum(1 for f in findings if f.get("suppressed"))
        suppressed_sarif = sum(1 for r in run.get("results", []) if r.get("suppressions"))
        if suppressed_json != suppressed_sarif:
            problems.append(f"suppressed findings differ: JSON {suppressed_json}, SARIF {suppressed_sarif}")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[0], "r", encoding="utf-8") as handle:
        document = json.load(handle)
    report = None
    if len(argv) >= 3 and argv[1] == "--json":
        with open(argv[2], "r", encoding="utf-8") as handle:
            report = json.load(handle)
    schema = schema_errors(document)
    structural = structural_errors(document, report)
    if schema is None:
        print("schema: skipped (jsonschema not installed)")
    else:
        print(f"schema: {'valid' if not schema else str(len(schema)) + ' error(s)'} against {os.path.relpath(SCHEMA_PATH, REPO)}")
        for e in schema[:20]:
            print("  -", e)
    print(f"structure: {'ok' if not structural else str(len(structural)) + ' problem(s)'}")
    for e in structural[:20]:
        print("  -", e)
    return 0 if not structural and not schema else 1


if __name__ == "__main__":
    sys.exit(main())
