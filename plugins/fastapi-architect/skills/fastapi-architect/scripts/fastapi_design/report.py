"""Architecture and verification report (v2.0): ``architecture_report/1.0`` as JSON, text or markdown."""

from __future__ import annotations

import json
from typing import Any, Dict, List

REPORT_SCHEMA = "architecture_report/1.0"


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_text(report: Dict[str, Any]) -> str:
    rec = report["recommendation"]
    gen = report.get("generation") or {}
    ver = report.get("verification") or {}
    lines = [
        f"architecture report ({report['tool']['name']} {report['tool']['version']}, {report['schema']})",
        f"spec: {report['spec']['name']} ({report['spec']['digest'][:12]}), {report['spec']['modules']} module(s), {report['spec']['entities']} entit{'y' if report['spec']['entities'] == 1 else 'ies'}, persistence {report['spec']['persistence']}, authentication {report['spec']['authentication']}",
        f"style: {rec['style']}",
    ]
    lines += [f"  - {r}" for r in rec["rationale"]]
    for w in rec.get("warnings", []):
        lines.append(f"  ! {w}")
    lines.append("rejected: " + "; ".join(f"{r['style']} ({r['reason'].split(';')[0]})" for r in rec["rejected"]))
    lines.append(f"explicit requirements: {report['spec']['explicit_paths']}  assumptions: {len(rec['assumptions'])}  pending decisions: {len(rec['pending'])}")
    for p in rec["pending"]:
        lines.append(f"  ? {p}")
    if gen:
        lines.append(f"generation: {gen['target']}  written {len(gen['written'])}  unchanged {len(gen['unchanged'])}  conflicts {len(gen['conflicts'])}{'  (dry run)' if gen.get('dry_run') else ''}")
    audit = ver.get("audit")
    if audit:
        lines.append(f"audit: {audit['findings_total']} active finding(s) {audit['by_severity']} over {audit['files_scanned']} files" + ("; partial" if audit.get("partial_analysis") else ""))
        for f in audit["findings"][:10]:
            lines.append(f"  - {f['severity']} {f['rule']} {f['file']}:{f['line']}")
    tests = ver.get("tests")
    if tests:
        if tests.get("ran"):
            run = tests["run"]
            counts = run.get("counts") or {}
            lines.append(f"tests: {run['outcome']} ({run['runner']}; " + ", ".join(f"{k} {counts[k]}" for k in ("executed", "passed", "failed", "errors", "skipped") if counts.get(k) is not None) + f") via executor {tests['executor']['name']} ({tests['executor']['isolation_level']})")
        else:
            lines.append(f"tests: not executed ({tests.get('denied_reason')})")
    g = report.get("guarantees") or {}
    if g:
        mt = g["multi_tenant"]
        lines.append(f"guarantees: authentication {g['authentication'].split(':')[0]}; multi-tenant {'requested' if mt['requested'] else 'not requested'}" + (f" (declared {mt['strategy_declared']}, implemented {mt['strategy_implemented']}, binding {str(mt['principal_tenant_binding']).split(':')[0]}, RLS {mt['row_level_security']})" if mt["requested"] else "") + f"; resource authorization {g['resource_authorization'].split(' (')[0]}; migrations {g['persistence']['migrations'].split(':')[0]}")
    lines.append(f"note: {report['note']}")
    return "\n".join(lines) + "\n"


def render_markdown(report: Dict[str, Any]) -> str:
    rec = report["recommendation"]
    gen = report.get("generation") or {}
    ver = report.get("verification") or {}
    out: List[str] = [f"# Architecture report — {report['spec']['name']}", ""]
    out.append(f"Spec `{report['spec']['digest'][:12]}` ({report['spec']['schema']}): {report['spec']['modules']} module(s), {report['spec']['entities']} entit{'y' if report['spec']['entities'] == 1 else 'ies'}, persistence `{report['spec']['persistence']}`, authentication `{report['spec']['authentication']}`, multi-tenant `{report['spec']['multi_tenant']}`.")
    out += ["", f"## Recommended style: `{rec['style']}`", ""]
    out += [f"- {r}" for r in rec["rationale"]]
    for w in rec.get("warnings", []):
        out.append(f"- **Warning:** {w}")
    out += ["", "### Rejected alternatives", ""]
    out += [f"- `{r['style']}`: {r['reason']}" for r in rec["rejected"]]
    out += ["", "### Requirements, assumptions and pending decisions", ""]
    out.append(f"Explicit values in the spec: {report['spec']['explicit_paths']}. Assumed defaults: {len(rec['assumptions'])}. Pending decisions: {len(rec['pending'])}.")
    if rec["assumptions"]:
        out += ["", "Assumptions:", ""] + [f"- {a}" for a in rec["assumptions"]]
    if rec["pending"]:
        out += ["", "Pending decisions:", ""] + [f"- {p}" for p in rec["pending"]]
    for key, title in (("persistence_notes", "Persistence"), ("security_notes", "Security"), ("testing_notes", "Testing")):
        if rec.get(key):
            out += ["", f"### {title}", ""] + [f"- {n}" for n in rec[key]]
    g = report.get("guarantees") or {}
    if g:
        mt = g["multi_tenant"]
        out += ["", "## Guarantees and limits", "", f"- Authentication: {g['authentication']}", f"- Resource authorization: {g['resource_authorization']}"]
        if mt["requested"]:
            out += [f"- Multi-tenant: declared strategy `{mt['strategy_declared']}`, implemented `{mt['strategy_implemented']}`; principal-tenant binding: {mt['principal_tenant_binding']}; storage isolation: {'; '.join(mt['storage_isolation'])}; row-level security: {mt['row_level_security']}; schema or database per tenant: {mt['schema_or_database_per_tenant']}"]
        out += [f"- Persistence: {g['persistence']['migrations']}; create_all used: {g['persistence']['create_all_used']}; constraint violation on commit: {g['persistence']['constraint_violation_on_commit']}", f"- Execution: {g['execution']}", f"- Not claimed: {', '.join(g['not_claimed'])}"]
    if report.get("layout"):
        out += ["", "## Layout", "", "```", *report["layout"], "```"]
    if gen:
        out += ["", "## Generation", "", f"Target `{gen['target']}`: {len(gen['written'])} written, {len(gen['unchanged'])} unchanged, {len(gen['conflicts'])} conflicts{' (dry run)' if gen.get('dry_run') else ''}. Manifest `{gen['manifest'].get('schema')}` with {len(gen['manifest'].get('files', {}))} file digests."]
    audit = ver.get("audit")
    if audit:
        out += ["", "## Audit of the generated project", "", f"{audit['findings_total']} active finding(s) {audit['by_severity']} over {audit['files_scanned']} files (schema {audit['schema_version']})."]
        if audit["findings"]:
            out += [""] + [f"- `{f['severity']}` `{f['rule']}` `{f['file']}:{f['line']}`: {f['message']}" for f in audit["findings"]]
        if audit.get("contracts"):
            out += ["", f"Route contracts: {audit['contracts']}"]
    tests = ver.get("tests")
    if tests:
        out += ["", "## Generated tests", ""]
        if tests.get("ran"):
            run = tests["run"]
            out.append(f"Outcome `{run['outcome']}` ({run['runner']}, counts {run.get('counts')}) through executor `{tests['executor']['name']}` ({tests['executor']['isolation_level']}); not provided by this executor: {'; '.join(tests['executor']['not_provided'])}.")
        else:
            out.append(f"Not executed: {tests.get('denied_reason')}.")
    out += ["", f"> {report['note']}"]
    return "\n".join(out) + "\n"
