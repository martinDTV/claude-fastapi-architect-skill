"""Text and Markdown rendering of a remediation run (v1.9)."""

from __future__ import annotations

import json
from typing import Any, Dict, List


def to_json(run: Dict[str, Any]) -> str:
    return json.dumps(run, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _run_line(label: str, tests: Dict[str, Any]) -> str:
    if not tests.get("ran"):
        return f"{label}: not executed" + (f" ({tests['refused']})" if tests.get("refused") else "")
    counts = tests.get("counts") or {}
    shown = ", ".join(f"{k} {counts[k]}" for k in ("executed", "passed", "failed", "errors", "skipped", "collection_errors") if counts.get(k) is not None)
    return f"{label}: {tests.get('outcome')} (exit {tests.get('returncode')}, {tests.get('runner')}{'; ' + shown if shown else '; no verifiable evidence'})"


def render_text(run: Dict[str, Any]) -> str:
    s = run["summary"]
    ex = run["execution"]
    lines = [
        f"remediation run ({run['tool']['name']} {run['tool']['version']}, {run['schema']})",
        f"project: {run['project_root']}  (untouched: {'yes' if run['original_untouched'] else 'NO'})",
        f"authorization: execution={'yes' if run['authorization']['execution'] else 'no'}, trust={run['authorization']['trust']}, scope={run['authorization']['scope'] or '-'}",
        (
            f"execution: granted to executor '{ex['executor']['name']}' ({ex['executor']['isolation_level']}); not provided: {'; '.join(ex['executor']['not_provided'])}"
            if ex["granted"] else f"execution: {ex['denied_reason'] or 'not authorized'} [{ex.get('denied_code') or '-'}]"
        ),
        f"cases: {s['cases']}  " + ", ".join(f"{k} {v}" for k, v in s["by_state"].items()) + (f"  composition: {s['composition']}" if s.get("composition") not in (None, "not_run") else ""),
    ]
    for case in run["cases"]:
        lines.append(f"- {case['state']:<12} {case['rule']} {case['file']}:{case['line']} [{case.get('recipe') or 'manual'}; {case.get('reason_code') or '-'}] {case.get('reason') or ''}")
        if case.get("reproducer"):
            lines.append(f"    reproducer ({case.get('reproducer_scope')}): {case['reproducer']}")
            lines.append(f"    {_run_line('before', case['linked_tests']['before'])}; {_run_line('after', case['linked_tests']['after'])}")
            if case.get("flipped_tests"):
                lines.append(f"    flipped: {', '.join(case['flipped_tests'][:5])}")
        if case.get("patch"):
            lines.append(f"    candidate {case['candidate_id']}: {case['patch']['summary']}")
    combined = run.get("combined") or {}
    if combined.get("state") != "not_run":
        suite = combined.get("suite") or {}
        lines.append(f"composition ({len(combined.get('cases', []))} candidates on one copy): {combined.get('state')}" + (f" - {combined.get('cause')}" if combined.get("cause") else "") + f"; {_run_line('suite before', suite.get('before', {}))}; {_run_line('suite after', suite.get('after', {}))}")
    lines.append(f"note: {run['note']}")
    return "\n".join(lines) + "\n"


def render_markdown(run: Dict[str, Any]) -> str:
    s = run["summary"]
    ex = run["execution"]
    out: List[str] = [f"# Remediation run — {run['tool']['name']} {run['tool']['version']}", ""]
    out.append(f"Project `{run['project_root']}` (untouched: **{'yes' if run['original_untouched'] else 'NO'}**). Trust `{run['authorization']['trust']}`; " + (f"executor `{ex['executor']['name']}` ({ex['executor']['isolation_level']})." if ex["granted"] else f"execution denied: {ex['denied_reason']}."))
    out.append("")
    out.append("| State | Cases |")
    out.append("|---|---|")
    for state, count in s["by_state"].items():
        out.append(f"| {state} | {count} |")
    out.append("")
    for case in run["cases"]:
        out.append(f"## {case['rule']} `{case['file']}:{case['line']}` — {case['state']} (`{case.get('reason_code') or '-'}`)")
        out.append("")
        out.append(f"Recipe `{case.get('recipe') or 'manual'}`, candidate `{case.get('candidate_id') or '-'}`, base `{(case.get('base_sha256') or '')[:12]}`. {case.get('reason') or ''}")
        if case.get("reproducer"):
            out.append("")
            out.append(f"Reproducer (`{case.get('reproducer_scope')}`) `{case['reproducer']}`: {_run_line('before', case['linked_tests']['before'])}; {_run_line('after', case['linked_tests']['after'])}." + (f" Flipped: `{'`, `'.join(case['flipped_tests'][:5])}`." if case.get("flipped_tests") else ""))
        if case.get("invariants"):
            out.append("")
            for inv in case["invariants"]:
                mark = "✓" if inv["satisfied"] else ("✗" if inv["satisfied"] is False else "·")
                out.append(f"- {mark} {inv['statement']} ({inv['checked_by']})")
        if case.get("limitations"):
            out.append("")
            out.append("Limitations: " + "; ".join(case["limitations"]))
        if case.get("patch"):
            out.append("")
            out.append("```diff")
            out.append(case["patch"]["diff"].rstrip("\n"))
            out.append("```")
        out.append("")
    out.append(f"> {run['note']}")
    return "\n".join(out) + "\n"
