"""Serialization: JSON, text, Markdown, and baseline comparison."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from .model import ENV_APPLICATION, ENV_MIGRATION, ENV_TEST, RULES, SCHEMA_VERSION, SEVERITY_ORDER, TOOL_NAME, TOOL_VERSION

_DIGITS = re.compile(r"\d+")


def to_json(report: Dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def fingerprint_of(finding: Dict[str, Any]) -> str:
    """Fingerprint for a finding dict; recomputed for baselines that predate schema 1.2."""
    if finding.get("fingerprint"):
        return str(finding["fingerprint"])
    raw = "|".join(
        [
            str(finding.get("rule", "")),
            str(finding.get("file", "")),
            str(finding.get("function") or ""),
            str(finding.get("symbol") or ""),
            _DIGITS.sub("#", str(finding.get("message", ""))),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


COMPARISON_NOTE = (
    "'fixed' and 'no_longer_detected' list findings the current run did not report. That is not evidence "
    "that the defect was corrected: the code may have moved, the file may have been skipped, the rule may have "
    "changed, or the analysis may be partial. Confirm with the recommended test of each finding."
)


def _brief(fp: str, f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fingerprint": fp,
        "rule": f.get("rule"),
        "file": f.get("file"),
        "line": f.get("line"),
        "severity": f.get("severity"),
        "confidence": f.get("confidence"),  # schema 1.6
        "evidence_state": f.get("evidence_state"),  # schema 1.6 (None for baselines before 1.5)
    }


def compare_reports(report: Dict[str, object], baseline: Dict[str, Any], name: Optional[str]) -> Dict[str, Any]:
    """Compare two audit reports by fingerprint (v1.8). Keeps the v1.4 keys (``new``, ``fixed``, ``unchanged``
    over non-suppressed findings) and adds persistent findings, severity/confidence/evidence changes,
    suppression transitions and a reliability flag. Wording: a finding that is not reported any more is
    *no longer detected*; the tool never claims it was resolved."""
    current_all = [f for f in report["findings"] if isinstance(f, dict)]  # type: ignore[index]
    previous_all = [f for f in baseline.get("findings", []) if isinstance(f, dict)]
    cur_all = {fingerprint_of(f): f for f in current_all}
    prev_all = {fingerprint_of(f): f for f in previous_all}
    cur_map = {fp: f for fp, f in cur_all.items() if not f.get("suppressed")}
    prev_map = {fp: f for fp, f in prev_all.items() if not f.get("suppressed")}
    sort_key = lambda d: (str(d["file"]), int(d["line"] or 0), str(d["rule"]))  # noqa: E731
    new = sorted((_brief(fp, f) for fp, f in cur_map.items() if fp not in prev_map), key=sort_key)
    fixed = sorted((dict(_brief(fp, f), status="no_longer_detected") for fp, f in prev_map.items() if fp not in cur_map), key=sort_key)
    persistent: List[Dict[str, Any]] = []
    severity_changes: List[Dict[str, Any]] = []
    confidence_changes: List[Dict[str, Any]] = []
    evidence_changes: List[Dict[str, Any]] = []
    for fp in cur_map:
        if fp not in prev_map:
            continue
        cur, prev = cur_map[fp], prev_map[fp]
        item = dict(_brief(fp, cur), baseline_line=prev.get("line"))
        persistent.append(item)
        for field_name, bucket in (("severity", severity_changes), ("confidence", confidence_changes), ("evidence_state", evidence_changes)):
            before, after = prev.get(field_name), cur.get(field_name)
            if before is not None and after is not None and before != after:
                bucket.append({"fingerprint": fp, "rule": cur.get("rule"), "file": cur.get("file"), "line": cur.get("line"), "from": before, "to": after})
    persistent.sort(key=sort_key)
    for bucket in (severity_changes, confidence_changes, evidence_changes):
        bucket.sort(key=sort_key)
    newly_suppressed = sorted(
        (dict(_brief(fp, f), suppression=f.get("suppression")) for fp, f in cur_all.items() if f.get("suppressed") and fp in prev_all and not prev_all[fp].get("suppressed")),
        key=sort_key,
    )
    unsuppressed = sorted(
        (_brief(fp, f) for fp, f in cur_all.items() if not f.get("suppressed") and fp in prev_all and prev_all[fp].get("suppressed")),
        key=sort_key,
    )
    new_suppressed = sorted((dict(_brief(fp, f), suppression=f.get("suppression")) for fp, f in cur_all.items() if f.get("suppressed") and fp not in prev_all), key=sort_key)
    still_reported = {item["fingerprint"] for item in newly_suppressed}
    no_longer_detected = [item for item in fixed if item["fingerprint"] not in still_reported]  # suppressed is still detected
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    bsummary = baseline.get("summary") if isinstance(baseline.get("summary"), dict) else {}
    partial_current = bool(summary.get("partial_analysis"))  # type: ignore[union-attr]
    partial_baseline = bool(bsummary.get("partial_analysis"))
    baseline_tool = baseline.get("tool", {}).get("version") if isinstance(baseline.get("tool"), dict) else None
    return {
        "baseline": name,
        "baseline_schema": baseline.get("schema_version"),
        "baseline_tool_version": baseline_tool,  # schema 1.6
        "current_tool_version": TOOL_VERSION,  # schema 1.6
        "counts": {"new": len(new), "fixed": len(fixed), "unchanged": len(persistent)},  # v1.4 contract, unchanged
        "changes": {  # schema 1.6
            "no_longer_detected": len(no_longer_detected),  # fixed minus the findings that are still reported but newly suppressed
            "persistent": len(persistent),
            "severity_changes": len(severity_changes),
            "confidence_changes": len(confidence_changes),
            "evidence_changes": len(evidence_changes),
            "newly_suppressed": len(newly_suppressed),
            "unsuppressed": len(unsuppressed),
            "new_suppressed": len(new_suppressed),
        },
        "new": new,
        "fixed": fixed,
        "no_longer_detected": no_longer_detected,  # schema 1.6
        "persistent": persistent,  # schema 1.6
        "severity_changes": severity_changes,  # schema 1.6
        "confidence_changes": confidence_changes,  # schema 1.6
        "evidence_changes": evidence_changes,  # schema 1.6
        "suppression_changes": {"newly_suppressed": newly_suppressed, "unsuppressed": unsuppressed, "new_suppressed": new_suppressed},  # schema 1.6
        "partial": {  # schema 1.6
            "current": partial_current,
            "baseline": partial_baseline,
            "comparison_reliable": not (partial_current or partial_baseline) and baseline_tool is not None,
        },
        "tool_changed": bool(baseline_tool) and baseline_tool != TOOL_VERSION,  # schema 1.6: rule changes may explain new/no-longer-detected
        "note": COMPARISON_NOTE,
    }


def compare_with_baseline(report: Dict[str, object], baseline: Dict[str, Any], name: Optional[str]) -> Dict[str, Any]:
    """Classify current non-suppressed findings as new/unchanged against a previous report (v1.4 entry point)."""
    return compare_reports(report, baseline, name)


def render_comparison_text(comparison: Dict[str, Any]) -> List[str]:
    counts = comparison["counts"]
    lines = [f"baseline {comparison.get('baseline')}: new {counts['new']}, fixed {counts['fixed']}, unchanged {counts['unchanged']}"]
    c = comparison.get("changes")
    if isinstance(c, dict):  # schema 1.6 comparison
        tools = f"{comparison.get('baseline_tool_version') or '?'} -> {comparison.get('current_tool_version')}"
        reliable = comparison.get("partial", {}).get("comparison_reliable")
        lines.append(
            f"  comparison (tool {tools}): no longer detected {c['no_longer_detected']}, persistent {c['persistent']} "
            f"(severity changes {c['severity_changes']}, confidence changes {c['confidence_changes']}, evidence changes {c['evidence_changes']}), "
            f"newly suppressed {c['newly_suppressed']}, unsuppressed {c['unsuppressed']}; reliable: {'yes' if reliable else 'no (partial analysis or unknown baseline tool)'}"
        )
        lines.append("  note: no longer detected is not resolved; confirm with each finding's recommended test")
        for label, items in (("new", comparison["new"]), ("no longer detected", comparison["no_longer_detected"])):
            for item in items:
                lines.append(f"  {label}: {item['file']}:{item['line']} {item['rule']} ({item['severity']}, {item.get('confidence')})")
        for label, items in (("severity", comparison["severity_changes"]), ("confidence", comparison["confidence_changes"]), ("evidence", comparison["evidence_changes"])):
            for item in items:
                lines.append(f"  {label} change: {item['file']}:{item['line']} {item['rule']} {item['from']} -> {item['to']}")
        for item in comparison["suppression_changes"]["newly_suppressed"]:
            lines.append(f"  newly suppressed: {item['file']}:{item['line']} {item['rule']} ({item.get('suppression')})")
        for item in comparison["suppression_changes"]["unsuppressed"]:
            lines.append(f"  unsuppressed: {item['file']}:{item['line']} {item['rule']}")
    return lines


def render_comparison_markdown(comparison: Dict[str, Any]) -> List[str]:
    counts = comparison["counts"]
    out = ["", f"## Comparison with baseline `{comparison.get('baseline')}`", ""]
    out.append(f"New: {counts['new']}, fixed: {counts['fixed']}, unchanged: {counts['unchanged']}.")
    c = comparison.get("changes")
    if isinstance(c, dict):
        out.append("")
        out.append(
            f"Tool {comparison.get('baseline_tool_version') or '?'} -> {comparison.get('current_tool_version')}. "
            f"No longer detected: {c['no_longer_detected']}; persistent: {c['persistent']}; severity changes: {c['severity_changes']}; "
            f"confidence changes: {c['confidence_changes']}; evidence changes: {c['evidence_changes']}; newly suppressed: {c['newly_suppressed']}; "
            f"unsuppressed: {c['unsuppressed']}. Reliable: {'yes' if comparison.get('partial', {}).get('comparison_reliable') else 'no'}."
        )
        out.append("")
        out.append(f"> {comparison.get('note')}")
    for label, items in (("New", comparison["new"]), ("No longer detected" if isinstance(c, dict) else "Fixed", comparison["fixed"])):
        if items:
            out.append("")
            out.append(f"### {label}")
            for item in items:
                out.append(f"- `{item['file']}:{item['line']}` {item['rule']} ({item['severity']}{', ' + item['confidence'] if item.get('confidence') else ''})")
    if isinstance(c, dict):
        for label, items in (("Severity changes", comparison["severity_changes"]), ("Confidence changes", comparison["confidence_changes"]), ("Evidence changes", comparison["evidence_changes"])):
            if items:
                out.append("")
                out.append(f"### {label}")
                for item in items:
                    out.append(f"- `{item['file']}:{item['line']}` {item['rule']}: {item['from']} -> {item['to']}")
        sc = comparison["suppression_changes"]
        if sc["newly_suppressed"] or sc["unsuppressed"]:
            out.append("")
            out.append("### Suppression changes")
            for item in sc["newly_suppressed"]:
                out.append(f"- newly suppressed `{item['file']}:{item['line']}` {item['rule']} ({item.get('suppression')})")
            for item in sc["unsuppressed"]:
                out.append(f"- unsuppressed `{item['file']}:{item['line']}` {item['rule']}")
    return out


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def render_text(report: Dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    sev = summary["by_severity"]
    tool = report["tool"]
    assert isinstance(tool, dict)
    env = summary["by_environment"]
    lines = [
        f"FastAPI static audit ({TOOL_NAME} {TOOL_VERSION}, schema {SCHEMA_VERSION}, python {tool['python']})",
        f"root: {report['project_root']}",
        (
            f"files scanned: {report['files_scanned']}  "
            f"skipped: {len(report['files_skipped'])}  "  # type: ignore[arg-type]
            f"findings: {summary['findings_total']} "
            f"(high {sev['high']}, medium {sev['medium']}, low {sev['low']}, info {sev['info']})"
            f"  partial analysis: {'yes' if summary['partial_analysis'] else 'no'}"
        ),
        (
            f"environment: application {env.get(ENV_APPLICATION, 0)}, test {env.get(ENV_TEST, 0)}, "
            f"migration {env.get(ENV_MIGRATION, 0)}  suppressed: {summary['suppressed']}  "
            f"tests excluded: {report['tests_excluded']}"
        ),
        "categories: " + ", ".join(f"{k} {v}" for k, v in summary["by_category"].items() if v),
    ]
    facts = report.get("facts")
    if isinstance(facts, dict):
        il = facts.get("import_linter")
        mr = facts.get("modules_root") or {}
        lines.append(
            "facts: "
            + ", ".join(
                [
                    f"fastapi={'yes' if facts.get('uses_fastapi') else 'no'}",
                    f"sqlalchemy={'async' if facts.get('uses_async_sqlalchemy') else 'yes' if facts.get('uses_sqlalchemy') else 'no'}",
                    f"alembic={'yes' if facts.get('uses_alembic') else 'no'}",
                    f"session factories={facts.get('session_factories', 0)}",
                    f"modules root={mr.get('path') or 'none'} ({mr.get('source')})",
                    f"import-linter={'yes' if il else 'no'}",
                ]
            )
        )
    inter = report.get("interprocedural")
    if isinstance(inter, dict) and inter.get("enabled"):
        res = inter.get("resolution") or {}
        cov = inter.get("coverage") or {}
        unc = inter.get("uncertainty") or {}
        lines.append(
            f"call graph: {inter.get('functions', 0)} functions, {inter.get('call_sites', 0)} call sites "
            f"(project {res.get('project', 0)}, external {res.get('external', 0)}, builtin {res.get('builtin', 0)}, "
            f"object {res.get('object', 0)}, ambiguous {res.get('ambiguous', 0)}, dynamic {res.get('dynamic', 0)}, "
            f"not found {res.get('not_found', 0)}), depth {inter.get('max_depth')}, "
            f"params typed via dependencies {inter.get('dependency_params', 0)} / callers {inter.get('inferred_params', 0)}, "
            f"attribute kinds {inter.get('attribute_kinds', 0)}"
            + ("  TRUNCATED: " + str(inter.get("truncated_reason")) if inter.get("truncated") else "")
        )
        if cov:
            lines.append(
                f"coverage: project-call {cov.get('project_call_coverage')} of {cov.get('resolvable_sites', 0)} resolvable sites, "
                f"classified {cov.get('classified_ratio')}; uncertainty: {unc.get('ambiguous_sites', 0)} ambiguous, "
                f"{unc.get('dynamic_sites', 0)} dynamic, {unc.get('callback_params_unresolved', 0)} callbacks unresolved, "
                f"{unc.get('interfaces', 0)} interfaces"
                + (f"; summaries skipped {inter.get('files_skipped')} file(s)" if inter.get("files_skipped") else "")
            )
    elif isinstance(inter, dict):
        lines.append("call graph: disabled (--no-interprocedural)")
    states = summary.get("by_evidence_state") if isinstance(summary, dict) else None
    if isinstance(states, dict):
        lines.append("evidence states: " + ", ".join(f"{k} {v}" for k, v in states.items() if v))
    contracts = report.get("contracts")
    if isinstance(contracts, dict) and contracts.get("summary", {}).get("routes"):
        cs = contracts["summary"]
        lines.append(
            f"contracts: {cs['routes']} routes, authenticated {cs['authenticated']}, public {cs['public']}, "
            f"unauthenticated and not public {cs['unauthenticated_not_public']}, authorization visible {cs['authorization_visible']}, "
            f"authorization unknown {cs['authorization_unknown']}"
        )
    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        lines.extend(render_comparison_text(comparison))
    findings = report["findings"]
    assert isinstance(findings, list)
    active = [item for item in findings if not item["suppressed"]]
    suppressed = [item for item in findings if item["suppressed"]]
    for title, items in (("", active), ("suppressed by documented markers (kept for review):", suppressed)):
        if not items:
            continue
        lines.append("")
        if title:
            lines.append(title)
        for item in items:
            evidence = item.get("evidence")
            tag = f"/{evidence}" if evidence and evidence != "verified" else ""
            lines.append(
                f"{item['file']}:{item['line']}:{item['column']}  {item['rule']}  "
                f"[{item['severity']}/{item['confidence']}/{item['environment']}{tag}]  "
                f"{item['message']}"
                + (f"  (suppressed: {item['suppression']})" if item["suppressed"] else "")
            )
            if item.get("call_path"):
                lines.append("    call path: " + " -> ".join(item["call_path"]))
    lines.append("")
    lines.append("disclaimer: " + str(report["disclaimer"]))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    tool = report["tool"]
    assert isinstance(tool, dict)
    findings = report["findings"]
    assert isinstance(findings, list)
    out: List[str] = []
    out.append(f"# FastAPI static audit ({TOOL_NAME} {TOOL_VERSION})")
    out.append("")
    out.append(f"- Root: `{report['project_root']}`")
    inter = report.get("interprocedural")
    if isinstance(inter, dict) and inter.get("enabled"):
        res = inter.get("resolution") or {}
        cov = inter.get("coverage") or {}
        out.append(
            f"- Call graph: {inter.get('call_sites', 0)} call sites, {res.get('project', 0)} project edges, "
            f"coverage {cov.get('project_call_coverage')} (classified {cov.get('classified_ratio')}), "
            f"{res.get('ambiguous', 0)} ambiguous, {res.get('dynamic', 0)} dynamic"
            + (" — truncated: " + str(inter.get("truncated_reason")) if inter.get("truncated") else "")
        )
    out.append(f"- Schema {SCHEMA_VERSION}, parsed with Python {tool['python']}")
    out.append(f"- Files scanned: {report['files_scanned']}; skipped: {len(report['files_skipped'])}; tests excluded: {report['tests_excluded']}; partial analysis: {'yes' if summary['partial_analysis'] else 'no'}")  # type: ignore[arg-type]
    out.append(f"- Findings: {summary['findings_total']} (suppressed: {summary['suppressed']})")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append("| Severity | Count |")
    out.append("|---|---|")
    for level in sorted(SEVERITY_ORDER, key=lambda k: -SEVERITY_ORDER[k]):
        out.append(f"| {level} | {summary['by_severity'][level]} |")
    out.append("")
    out.append("| Category | Count |")
    out.append("|---|---|")
    for cat, count in summary["by_category"].items():
        out.append(f"| {cat} | {count} |")
    out.append("")
    out.append("| Verification | Count |")
    out.append("|---|---|")
    for key, count in summary["by_verification"].items():
        out.append(f"| {key} | {count} |")
    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        out.extend(render_comparison_markdown(comparison))
    active = [f for f in findings if not f["suppressed"]]
    suppressed = [f for f in findings if f["suppressed"]]
    out.append("")
    out.append("## Findings")
    if not active:
        out.append("")
        out.append("No active findings.")
    for level in sorted(SEVERITY_ORDER, key=lambda k: -SEVERITY_ORDER[k]):
        group = [f for f in active if f["severity"] == level]
        if not group:
            continue
        out.append("")
        out.append(f"### {level} ({len(group)})")
        out.append("")
        for f in group:
            where = f"`{f['file']}:{f['line']}`"
            fn = f" in `{f['function']}`" if f.get("function") else ""
            out.append(f"- {where} **{f['rule']}** [{f['confidence']} confidence, {f['environment']}, {f['verification']}]{fn}: {_md_escape(f['message'])}")
            if f.get("recommended_test"):
                out.append(f"  - Recommended check: {_md_escape(f['recommended_test'])}")
    if suppressed:
        out.append("")
        out.append(f"## Suppressed by documented markers ({len(suppressed)})")
        out.append("")
        out.append("| Location | Rule | Suppression |")
        out.append("|---|---|---|")
        for f in suppressed:
            out.append(f"| `{f['file']}:{f['line']}` | {f['rule']} | {_md_escape(str(f['suppression']))} |")
    facts = report.get("facts")
    if isinstance(facts, dict):
        out.append("")
        out.append("## Project facts (inferred)")
        out.append("")
        mr = facts.get("modules_root") or {}
        layers = facts.get("layers") or {}
        il = facts.get("import_linter")
        out.append(f"- FastAPI: {facts.get('uses_fastapi')}; SQLAlchemy: {facts.get('uses_sqlalchemy')} (async: {facts.get('uses_async_sqlalchemy')}); Alembic: {facts.get('uses_alembic')}")
        out.append(f"- Session factories: {facts.get('session_factories')}; Annotated session aliases: {facts.get('session_aliases')}")
        out.append(f"- Routers with dependencies: {facts.get('protected_routers')}; application-level auth signal: {facts.get('global_auth_signal')}")
        out.append(f"- Business modules root: {mr.get('path') or 'none'} ({mr.get('source')}); layers: {layers.get('source')} {json.dumps(layers.get('counts') or {}, sort_keys=True)}")
        out.append(f"- import-linter: {json.dumps(il, sort_keys=True) if il else 'not found'}")
        if facts.get("config_source"):
            out.append(f"- Config: `{facts['config_source']}`")
    out.append("")
    out.append("## Disclaimer")
    out.append("")
    out.append(str(report["disclaimer"]))
    return "\n".join(out) + "\n"


def list_rules_text() -> str:
    lines = []
    for rule, meta in sorted(RULES.items(), key=lambda kv: (kv[1]["category"], kv[0])):
        lines.append(f"{rule}  [{meta['severity']}/{meta['category']}/{meta['verification']}]  {meta['summary']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# SARIF 2.1.0 (v1.7): CI and code-scanning import. Every earlier format is kept.
# --------------------------------------------------------------------------- #

SARIF_SCHEMA = "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
SARIF_LEVELS = {"high": "error", "medium": "warning", "low": "note", "info": "note"}
INFORMATION_URI = "https://github.com/martinDTV/claude-fastapi-architect-skill"


def to_sarif(report: Dict[str, object]) -> str:
    """SARIF 2.1.0 document for ``report``: one run, one rule per catalogue entry used, one result per
    finding with the stable fingerprint as a partial fingerprint, documented suppressions as
    ``suppressions[].kind == "inSource"``, confidence/evidence/evidence_state/verification/call_path/
    recommended_test as result properties, and the interprocedural/contracts summaries as run
    properties. Deterministic: rules and results follow the report's order."""
    from .model import RULES

    findings = report["findings"]
    assert isinstance(findings, list)
    used = sorted({f["rule"] for f in findings})
    rule_index = {rule: i for i, rule in enumerate(used)}
    rules = []
    for rule in used:
        meta = RULES.get(rule, {})
        rules.append(
            {
                "id": rule,
                "name": rule.title().replace("_", ""),
                "shortDescription": {"text": rule.replace("_", " ").capitalize()},
                "fullDescription": {"text": meta.get("summary", "")},
                "defaultConfiguration": {"level": SARIF_LEVELS.get(meta.get("severity", "low"), "note")},
                "helpUri": INFORMATION_URI,
                "properties": {"category": meta.get("category", ""), "verification": meta.get("verification", ""), "tags": [meta.get("category", "")]},
            }
        )
    root = str(report.get("project_root", "")).rstrip("/") + "/"
    results = []
    for f in findings:
        result: Dict[str, object] = {
            "ruleId": f["rule"],
            "ruleIndex": rule_index[f["rule"]],
            "level": SARIF_LEVELS.get(f["severity"], "note"),
            "message": {"text": f["message"]},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["file"], "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": max(1, int(f["line"])), "startColumn": max(1, int(f["column"]) + 1)},
                    },
                    **({"logicalLocations": [{"name": f["function"], "kind": "function"}]} if f.get("function") else {}),
                }
            ],
            "partialFingerprints": {"fastapiArchitect/fingerprint/v1": f["fingerprint"]},
            "properties": {
                "confidence": f["confidence"],
                "evidence": f.get("evidence"),
                "evidence_state": f.get("evidence_state"),
                "verification": f.get("verification"),
                "environment": f.get("environment"),
                "category": f.get("category"),
                "symbol": f.get("symbol"),
                "call_path": f.get("call_path"),
                "recommended_test": f.get("recommended_test"),
            },
        }
        if f.get("suppressed"):
            result["suppressions"] = [{"kind": "inSource", "justification": str(f.get("suppression") or "")}]
        comparison = report.get("comparison")
        if isinstance(comparison, dict):
            new_fps = {item.get("fingerprint") for item in comparison.get("new", [])}
            # the comparison lists new and fixed fingerprints; a current finding that is not new is unchanged
            result["baselineState"] = "new" if f["fingerprint"] in new_fps else "unchanged"
        results.append(result)
    summary = report.get("summary")
    assert isinstance(summary, dict)
    document = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "semanticVersion": TOOL_VERSION,
                        "informationUri": INFORMATION_URI,
                        "rules": rules,
                    }
                },
                "originalUriBaseIds": {"%SRCROOT%": {"uri": "file://" + root}},
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "properties": {"partial_analysis": bool(summary.get("partial_analysis")), "files_scanned": report.get("files_scanned"), "files_skipped": report.get("files_skipped")},
                    }
                ],
                "results": results,
                "properties": {
                    "schema_version": report.get("schema_version"),
                    "summary": summary,
                    "interprocedural": report.get("interprocedural"),
                    "contracts_summary": (report.get("contracts") or {}).get("summary") if isinstance(report.get("contracts"), dict) else None,
                    "contracts_partition": (report.get("contracts") or {}).get("partition") if isinstance(report.get("contracts"), dict) else None,
                    "disclaimer": report.get("disclaimer"),
                },
            }
        ],
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
