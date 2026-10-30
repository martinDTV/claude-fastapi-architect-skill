#!/usr/bin/env python3
"""Verified remediation of audit findings in isolated copies (v1.9). Never writes to the project.

    python3 audit_remediate.py PROJECT [--select RULE ...] [--fingerprint FP ...]
                               [--authorize-execution --trust {fixture,trusted,untrusted} --trust-scope DIR
                                --link KEY=COMMAND ... [--test-command CMD]] [--timeout 600]
                               [--env-passthrough NAME,...] [--allow-command NAME] [--executor local]
                               [--workspace DIR] [--keep-workspace] [--patch-dir DIR]
                               [--output run.json] [--format text|json|markdown] [--strict] [--fail-on-rejected]

Static steps (audit, candidate patch, re-audit of the copy) never execute project code. Executing tests needs
``--authorize-execution`` **and** a trust profile with a scope: ``--trust fixture`` or ``--trust trusted`` grant the
local executor (process hygiene: copy, fresh HOME, reduced environment, no shell, allow-listed programs, closed
stdin, timeout, CPU/file-size limits; not a sandbox, no network isolation). ``--trust untrusted`` (the default)
fails closed: no executor can run it in this version. The trust flag is a declaration by the operator and is
recorded as such; it is not a technical guarantee.

Each finding gets its own copy and its own linked reproducer (``--link FINGERPRINT=CMD``, ``--link FILE:LINE=CMD``
or ``--link RULE=CMD``): ``verified`` needs the reproducer to fail before and pass after the patch, the finding to
disappear (not merely be suppressed), no new finding, and untouched test files; otherwise the case is
``blocked``, ``audit_clean`` (no test executed), ``inconclusive`` (insufficient or infrastructure evidence) or
``rejected``. ``--test-command`` is the suite-level command run in a combined copy with every candidate applied.

Exit codes: 0 run completed; 1 with --fail-on-rejected when a case was rejected, or with --strict when any case
is not verified; 2 invalid input or the project changed during the run.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi_audit.model import RULES, TOOL_NAME  # noqa: E402
from fastapi_audit.remediate import RECIPES, Authorization, RemediationError, RunOptions, run_remediation  # noqa: E402
from fastapi_audit.remediate.executors import REGISTRY, TRUST_PROFILES, TRUST_UNTRUSTED  # noqa: E402
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
    parser.add_argument("--authorize-execution", action="store_true", help="allow commands to run inside the isolated copies (requires --trust fixture|trusted with a scope)")
    parser.add_argument("--trust", choices=TRUST_PROFILES, default=TRUST_UNTRUSTED, help="trust profile of the project: fixture (plugin fixtures/examples), trusted (declared by the operator), untrusted (default: no local execution)")
    parser.add_argument("--trust-scope", metavar="DIR", help="directory the authorization covers (must contain the project); defaults to the project itself when --trust is not untrusted")
    parser.add_argument("--executor", default="local", help=f"executor to use (registered: {', '.join(sorted(REGISTRY))})")
    parser.add_argument("--link", action="append", default=[], metavar="KEY=COMMAND", help="reproducer command for one finding: KEY is a fingerprint, FILE:LINE or a rule id (repeatable)")
    parser.add_argument("--test-command", metavar="CMD", help="suite-level command run in the combined copy before and after all candidates (no shell; python/pytest/uv only unless --allow-command)")
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
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every case ends verified")
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
    if (args.test_command or args.link) and not args.authorize_execution:
        sys.stderr.write(f"{TOOL_NAME}: error: --test-command/--link require --authorize-execution (nothing is executed without it)\n")
        return 2
    links = {}
    for item in args.link:
        key, sep, command = item.partition("=")
        if not sep or not key.strip() or not command.strip():
            sys.stderr.write(f"{TOOL_NAME}: error: --link expects KEY=COMMAND, got {item!r}\n")
            return 2
        links[key.strip()] = command.strip()
    scope = args.trust_scope
    if scope is None and args.authorize_execution and args.trust != TRUST_UNTRUSTED:
        scope = args.path
    authorization = Authorization(
        execution=bool(args.authorize_execution),
        trust=args.trust,
        scope=os.path.abspath(scope) if scope else None,
        declared_by=f"cli: --authorize-execution --trust {args.trust}" + (f" --trust-scope {scope}" if args.trust_scope else "") if args.authorize_execution else None,
    )
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
        authorization=authorization,
        executor=args.executor,
        links=links,
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
    if args.strict and (run["summary"]["cases"] == 0 or set(run["summary"]["by_state"]) != {"verified"}):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
