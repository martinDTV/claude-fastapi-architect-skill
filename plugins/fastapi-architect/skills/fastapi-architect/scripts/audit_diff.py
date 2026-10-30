#!/usr/bin/env python3
"""Compare two saved audit reports (v1.8). Read-only; no project is re-analysed.

    python3 audit_diff.py BEFORE.json AFTER.json [--format text|json|markdown] [--fail-on-new] [--fail-on SEVERITY]

Findings are matched by fingerprint (rule, file, function, symbol, message shape), so line shifts do not
produce churn. The output lists new findings, findings that are *no longer detected*, persistent findings
with their severity/confidence/evidence changes, suppression transitions, and whether either report was
produced by a partial analysis (in which case the comparison is marked unreliable). The tool never says a
finding was resolved: not being detected any more is all a static re-run can show.

Exit codes: 0 ok; 1 when --fail-on-new and there are new non-suppressed findings (at --fail-on severity or
above when given); 2 on invalid input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi_audit.model import SEVERITY_ORDER, TOOL_NAME  # noqa: E402
from fastapi_audit.report import compare_reports, render_comparison_markdown, render_comparison_text  # noqa: E402


def _load(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"audit_diff: error: cannot read {path}: {exc}\n")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        sys.stderr.write(f"audit_diff: error: not an {TOOL_NAME} report (no 'findings' list): {path}\n")
        return None
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_diff", description="Compare two audit_fastapi JSON reports by fingerprint.")
    parser.add_argument("before", help="earlier JSON report")
    parser.add_argument("after", help="later JSON report")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--fail-on-new", action="store_true", help="exit 1 when the later report has new non-suppressed findings")
    parser.add_argument("--fail-on", choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get), help="severity floor for --fail-on-new")
    args = parser.parse_args(argv)
    before = _load(args.before)
    after = _load(args.after)
    if before is None or after is None:
        return 2
    comparison = compare_reports(after, before, os.path.basename(args.before))
    comparison["after"] = os.path.basename(args.after)
    if args.format == "json":
        sys.stdout.write(json.dumps(comparison, indent=2, sort_keys=True) + "\n")
    elif args.format == "markdown":
        sys.stdout.write("\n".join(render_comparison_markdown(comparison)).lstrip("\n") + "\n")
    else:
        sys.stdout.write("\n".join(render_comparison_text(comparison)) + "\n")
    if args.fail_on_new:
        floor = SEVERITY_ORDER[args.fail_on] if args.fail_on else 0
        if any(SEVERITY_ORDER.get(str(item.get("severity")), 0) >= floor for item in comparison["new"]):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
