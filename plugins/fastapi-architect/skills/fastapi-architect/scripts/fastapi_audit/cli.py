"""Command line interface. Keeps every v1.3 option and exit code."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence, Set

from .discovery import DEFAULT_MAX_FILE_BYTES
from .engine import AuditOptions, audit_project
from .model import CATEGORIES, RULES, SEVERITY_ORDER, TOOL_NAME, TOOL_VERSION
from .report import list_rules_text, render_markdown, render_text, to_json


def _split_csv(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Read-only static audit of a FastAPI project. Findings are heuristic "
            "review signals, not proven defects."
        ),
    )
    parser.add_argument("path", nargs="?", help="project directory or single .py file")
    parser.add_argument("--json", action="store_true", help="print the JSON report on stdout (same as --format json)")
    parser.add_argument(
        "--format", choices=("text", "json", "markdown"), default=None,
        help="output format on stdout (default text; --output always writes JSON)",
    )
    parser.add_argument("--output", metavar="FILE", help="write the JSON report to FILE (the only write this tool performs)")
    parser.add_argument("--exclude", action="append", default=[], metavar="DIRNAME", help="additional directory name to skip (repeatable)")
    parser.add_argument("--include-hidden", action="store_true", help="also scan dot-directories (the default exclusion list still applies)")
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES, help=f"skip files larger than this many bytes (default {DEFAULT_MAX_FILE_BYTES})")
    parser.add_argument(
        "--exclude-tests", action="store_true",
        help=(
            "do not analyse test directories (tests/, test/, testing/, __tests__/) or "
            "test-named files (test_*.py, *_test.py, conftest.py); off by default, test "
            "findings are otherwise reported with environment=test"
        ),
    )
    parser.add_argument(
        "--no-suppressions", action="store_true",
        help="ignore documented suppressions ('# noqa: ASYNC2xx', '# audit-fastapi: ignore') and report every finding as active",
    )
    parser.add_argument("--category", action="append", default=[], metavar="NAME", help=f"only run these rule categories (repeatable or comma-separated): {', '.join(CATEGORIES)}")
    parser.add_argument("--select", action="append", default=[], metavar="RULE", help="only report these rule ids (repeatable or comma-separated)")
    parser.add_argument("--ignore", action="append", default=[], metavar="RULE", help="never report these rule ids (repeatable or comma-separated)")
    parser.add_argument("--include-path", action="append", default=[], metavar="GLOB", help="only analyse files whose relative path matches GLOB (repeatable)")
    parser.add_argument("--exclude-path", action="append", default=[], metavar="GLOB", help="skip files whose relative path matches GLOB (repeatable)")
    parser.add_argument(
        "--changed-files", metavar="FILE",
        help="report findings only for the relative paths listed in FILE ('-' for stdin); the whole project is still analysed for context",
    )
    parser.add_argument("--baseline", metavar="FILE", help="previous JSON report; the output gains a new/fixed/unchanged comparison")
    parser.add_argument("--fail-on-new", action="store_true", help="exit 1 if the comparison with --baseline shows new non-suppressed findings (at --fail-on severity or above when given)")
    parser.add_argument("--config", metavar="FILE", help="JSON config file (default: .audit-fastapi.json at the project root, if present)")
    parser.add_argument(
        "--max-call-depth", type=int, default=4, metavar="N",
        help="maximum length of call paths followed by the interprocedural pass (default 4)",
    )
    parser.add_argument(
        "--time-budget", type=float, default=5.0, metavar="SECONDS",
        help="wall-clock budget for the interprocedural pass; when exceeded the report is still produced and marked truncated (default 5)",
    )
    parser.add_argument(
        "--no-interprocedural", action="store_true",
        help="skip the call-graph pass (no cross-function kinds, call paths or indirect blocking findings)",
    )
    parser.add_argument(
        "--fail-on", choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get),
        help="exit with status 1 if any non-suppressed finding has this severity or higher",
    )
    parser.add_argument("--exit-zero", action="store_true", help="always exit 0 after a successful run (CI reporting mode)")
    parser.add_argument("--list-rules", action="store_true", help="list rules and exit")
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    return parser


def _read_changed_files(spec: str) -> Set[str]:
    if spec == "-":
        text = sys.stdin.read()
    else:
        with open(spec, "r", encoding="utf-8") as handle:
            text = handle.read()
    return {line.strip().replace(os.sep, "/") for line in text.splitlines() if line.strip() and not line.startswith("#")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_rules:
        sys.stdout.write(list_rules_text())
        return 0
    if not args.path:
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{TOOL_NAME}: error: a project path is required\n")
        return 2

    categories = _split_csv(args.category)
    unknown = sorted(set(categories) - set(CATEGORIES))
    if unknown:
        sys.stderr.write(f"{TOOL_NAME}: error: unknown category: {', '.join(unknown)} (valid: {', '.join(CATEGORIES)})\n")
        return 2
    select = _split_csv(args.select)
    ignore = _split_csv(args.ignore)
    unknown_rules = sorted({r for r in [*select, *ignore] if r not in RULES})
    if unknown_rules:
        sys.stderr.write(f"{TOOL_NAME}: error: unknown rule id: {', '.join(unknown_rules)} (see --list-rules)\n")
        return 2

    only_files: Optional[Set[str]] = None
    baseline = None
    try:
        if args.changed_files:
            only_files = _read_changed_files(args.changed_files)
        if args.baseline:
            with open(args.baseline, "r", encoding="utf-8") as handle:
                baseline = json.load(handle)
            if not isinstance(baseline, dict) or "findings" not in baseline:
                sys.stderr.write(f"{TOOL_NAME}: error: baseline is not an audit report: {args.baseline}\n")
                return 2
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{TOOL_NAME}: error: cannot read input file: {exc}\n")
        return 2

    options = AuditOptions(
        excluded_dirs=tuple(args.exclude),
        include_hidden=args.include_hidden,
        max_file_bytes=args.max_file_bytes,
        exclude_tests=args.exclude_tests,
        honor_suppressions=not args.no_suppressions,
        categories=set(categories) if categories else None,
        select_rules=set(select) if select else None,
        ignore_rules=set(ignore),
        include_globs=tuple(args.include_path),
        exclude_globs=tuple(args.exclude_path),
        only_files=only_files,
        config_path=args.config,
        baseline=baseline,
        baseline_name=os.path.basename(args.baseline) if args.baseline else None,
        interprocedural=not args.no_interprocedural,
        max_call_depth=max(1, args.max_call_depth),
        time_budget_s=max(0.1, args.time_budget),
    )
    try:
        report = audit_project(args.path, options=options)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{TOOL_NAME}: error: path does not exist: {exc}\n")
        return 2
    except ValueError as exc:
        sys.stderr.write(f"{TOOL_NAME}: error: invalid config: {exc}\n")
        return 2

    fmt = args.format or ("json" if args.json else "text")
    payload = to_json(report)
    output_error = None
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            output_error = f"{TOOL_NAME}: error: cannot write report to {args.output}: {reason}"
    if fmt == "json":
        sys.stdout.write(payload)
    elif fmt == "markdown":
        sys.stdout.write(render_markdown(report))
    elif not args.output:
        sys.stdout.write(render_text(report))
    elif output_error is None:
        sys.stdout.write(f"report written to {args.output}\n")
    if output_error is not None:
        sys.stderr.write(output_error + "\n")
        return 2

    if args.exit_zero:
        return 0
    findings = report["findings"]
    assert isinstance(findings, list)
    threshold = SEVERITY_ORDER[args.fail_on] if args.fail_on else None
    if threshold is not None and any(
        SEVERITY_ORDER[f["severity"]] >= threshold and not f["suppressed"] for f in findings
    ):
        return 1
    comparison = report.get("comparison")
    if args.fail_on_new and isinstance(comparison, dict):
        floor = threshold if threshold is not None else 0
        if any(SEVERITY_ORDER[item["severity"]] >= floor for item in comparison["new"]):
            return 1
    return 0
