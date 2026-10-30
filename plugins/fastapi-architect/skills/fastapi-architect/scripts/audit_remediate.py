#!/usr/bin/env python3
"""Verified remediation of audit findings in an isolated copy (v1.9). Never writes to the project.

    python3 audit_remediate.py PROJECT [--select RULE ...] [--fingerprint FP ...]
                               [--authorize-execution --test-command "pytest -q"] [--timeout 600]
                               [--env-passthrough NAME,...] [--allow-command NAME]
                               [--workspace DIR] [--keep-workspace] [--patch-dir DIR]
                               [--output run.json] [--format text|json|markdown] [--fail-on-rejected]

The project is audited read-only, copied into a temporary workspace, and the findings with an automatic
recipe are patched **in the copy**; the copy is audited again and, only with ``--authorize-execution``, the
test command runs there before and after the patch with a sanitised environment, a fresh HOME, no stdin,
a timeout and resource limits. Each case ends in one state: blocked, audit_clean, verified or rejected (see
``references/safe-refactoring.md``). The patches are printed as unified diffs (``--patch-dir`` writes them);
applying them to the real project is a human decision (``git apply``).

Exit codes: 0 run completed; 1 with --fail-on-rejected when a case was rejected; 2 invalid input or the
project changed during the run.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi_audit.model import RULES, TOOL_NAME  # noqa: E402
from fastapi_audit.remediate import RECIPES, RemediationError, RunOptions, run_remediation  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ALLOWED_COMMANDS, DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_audit.remediate.render import render_markdown, render_text, to_json  # noqa: E402


def _csv(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_remediate", description="Patch audit findings in an isolated copy and verify before/after. The project itself is never modified.")
    parser.add_argument("path", help="project directory (audited read-only)")
    parser.add_argument("--select", action="append", default=[], metavar="RULE", help=f"only these rules (repeatable or comma-separated); rules with a recipe: {', '.join(sorted(RECIPES))}")
    parser.add_argument("--fingerprint", action="append", default=[], metavar="FP", help="only these finding fingerprints (repeatable or comma-separated)")
    parser.add_argument("--authorize-execution", action="store_true", help="allow the test command to run inside the isolated copy (before and after the patch)")
    parser.add_argument("--test-command", metavar="CMD", help="command executed in the copy when authorized (no shell; python/pytest/uv only unless --allow-command)")
    parser.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS", help="wall-clock limit per test run (default 600)")
    parser.add_argument("--env-passthrough", action="append", default=[], metavar="NAMES", help=f"environment variables handed to the command (default: {', '.join(DEFAULT_ENV_PASSTHROUGH)})")
    parser.add_argument("--allow-command", action="append", default=[], metavar="NAME", help=f"additional command names allowed to run (default: {', '.join(DEFAULT_ALLOWED_COMMANDS)})")
    parser.add_argument("--workspace", metavar="DIR", help="use this empty directory for the isolated copy instead of a temporary one")
    parser.add_argument("--keep-workspace", action="store_true", help="do not delete the isolated copy at the end (its path is reported)")
    parser.add_argument("--patch-dir", metavar="DIR", help="write one .diff per patched case into DIR")
    parser.add_argument("--include-tests", action="store_true", help="also patch findings located in test code")
    parser.add_argument("--max-cases", type=int, default=50, metavar="N")
    parser.add_argument("--config", metavar="FILE", help="auditor configuration (default: .audit-fastapi.json at the project root)")
    parser.add_argument("--output", metavar="FILE", help="write the JSON run report to FILE")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--fail-on-rejected", action="store_true", help="exit 1 when any case ends rejected")
    args = parser.parse_args(argv)

    select = _csv(args.select)
    unknown = sorted(r for r in select if r not in RULES)
    if unknown:
        sys.stderr.write(f"{TOOL_NAME}: error: unknown rule id: {', '.join(unknown)}\n")
        return 2
    without_recipe = sorted(r for r in select if r not in RECIPES)
    if without_recipe:
        sys.stderr.write(f"{TOOL_NAME}: error: no automatic recipe for: {', '.join(without_recipe)} (available: {', '.join(sorted(RECIPES))})\n")
        return 2
    if args.test_command and not args.authorize_execution:
        sys.stderr.write(f"{TOOL_NAME}: error: --test-command requires --authorize-execution (nothing is executed without it)\n")
        return 2
    policy = ExecutionPolicy(
        authorized=bool(args.authorize_execution),
        command=args.test_command,
        timeout_s=max(1.0, args.timeout),
        env_passthrough=tuple(DEFAULT_ENV_PASSTHROUGH) + tuple(_csv(args.env_passthrough)),
        allowed_commands=tuple(DEFAULT_ALLOWED_COMMANDS) + tuple(args.allow_command),
    )
    options = RunOptions(
        select_rules=set(select) if select else None,
        fingerprints=set(_csv(args.fingerprint)) if args.fingerprint else None,
        policy=policy,
        workspace_dir=args.workspace,
        keep_workspace=bool(args.keep_workspace),
        patch_dir=args.patch_dir,
        max_cases=max(1, args.max_cases),
        config_path=args.config,
        include_tests=bool(args.include_tests),
    )
    try:
        run = run_remediation(args.path, options)
    except (RemediationError, FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"{TOOL_NAME}: error: {exc}\n")
        return 2
    payload = to_json(run)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            sys.stderr.write(f"{TOOL_NAME}: error: cannot write {args.output}: {exc}\n")
            return 2
    if args.format == "json":
        sys.stdout.write(payload)
    elif args.format == "markdown":
        sys.stdout.write(render_markdown(run))
    else:
        sys.stdout.write(render_text(run))
    if args.fail_on_rejected and run["summary"]["by_state"].get("rejected"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
