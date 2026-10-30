"""Text and Markdown rendering of a remediation run (v1.9)."""

from __future__ import annotations

import json
from typing import Any, Dict, List


def to_json(run: Dict[str, Any]) -> str:
    return json.dumps(run, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _tests_line(label: str, tests: Dict[str, Any]) -> str:
    if not tests.get("ran"):
        return f"  tests {label}: not executed" + (f" ({tests['refused']})" if tests.get("refused") else "")
    status = "timed out" if tests.get("timed_out") else ("passed" if tests.get("passed") else f"failed (exit {tests.get('returncode')})")
    return f"  tests {label}: {status}"


def render_text(run: Dict[str, Any]) -> str:
    s = run["summary"]
    lines = [
        f"remediation run ({run['tool']['name']} {run['tool']['version']}, {run['schema']})",
        f"project: {run['project_root']}  (untouched: {'yes' if run['original_untouched'] else 'NO'})",
        f"workspace: copy{' kept at ' + run['workspace']['path'] if run['workspace'].get('path') else ' (removed)'}",
        f"execution: {'authorized' if run['authorization']['execution'] else 'not authorized (static steps only)'}"
        + (f", command: {run['authorization']['test_command']}" if run['authorization'].get('test_command') else ""),
        _tests_line("before", run["tests"]["before"]),
        _tests_line("after", run["tests"]["after"]),
        f"cases: {s['cases']}  " + ", ".join(f"{k} {v}" for k, v in s["by_state"].items()),
    ]
    if run.get("defect_observed_before_patch") is not None:
        lines.append(f"defect observed before the patch: {'yes (tests failed)' if run['defect_observed_before_patch'] else 'no (tests passed before the patch: the suite does not cover it)'}")
    for case in run["cases"]:
        lines.append(f"- {case['state']:<11} {case['rule']} {case['file']}:{case['line']} [{case.get('recipe') or 'manual'}] {case.get('reason') or ''}")
        if case.get("patch"):
            lines.append(f"    patch: {case['patch']['summary']}")
    lines.append(f"note: {run['note']}")
    return "\n".join(lines) + "\n"


def render_markdown(run: Dict[str, Any]) -> str:
    s = run["summary"]
    out: List[str] = [f"# Remediation run — {run['tool']['name']} {run['tool']['version']}", ""]
    out.append(f"Project `{run['project_root']}` (untouched: **{'yes' if run['original_untouched'] else 'NO'}**). Execution {'authorized' if run['authorization']['execution'] else 'not authorized: static steps only'}.")
    out.append("")
    out.append("| State | Cases |")
    out.append("|---|---|")
    for state, count in s["by_state"].items():
        out.append(f"| {state} | {count} |")
    out.append("")
    out.append(_tests_line("before", run["tests"]["before"]).strip())
    out.append("")
    out.append(_tests_line("after", run["tests"]["after"]).strip())
    out.append("")
    for case in run["cases"]:
        out.append(f"## {case['rule']} `{case['file']}:{case['line']}` — {case['state']}")
        out.append("")
        out.append(f"Recipe: `{case.get('recipe') or 'manual'}`. {case.get('reason') or ''}")
        if case.get("patch"):
            out.append("")
            out.append("```diff")
            out.append(case["patch"]["diff"].rstrip("\n"))
            out.append("```")
        out.append("")
    out.append(f"> {run['note']}")
    return "\n".join(out) + "\n"
