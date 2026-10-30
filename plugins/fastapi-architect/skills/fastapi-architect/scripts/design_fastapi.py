#!/usr/bin/env python3
"""Design and build a FastAPI backend from a declarative architecture contract (v2.0).

    python3 design_fastapi.py SPEC.json [--validate-only]
    python3 design_fastapi.py SPEC.json --target DIR [--allow-existing] [--overwrite] [--dry-run] [--no-audit]
                              [--authorize-execution --trust {fixture,trusted,untrusted} [--trust-scope DIR]
                               --test-command "python3 -m pytest -q"] [--timeout 600] [--env-passthrough NAMES]
                              [--allow-command NAME] [--executor local]
                              [--output report.json] [--format text|json|markdown] [--strict]

The specification is JSON (never executed) and is validated strictly: unknown properties, bad identifiers and
inconsistent choices are errors reported with their JSON path. The advisor recommends one structure (layered,
modular monolith, hexagonal, or microservices only when independent deployment is stated and justified) with its
reasons, the rejected alternatives, the assumptions it made and the pending decisions. The generator writes a
runnable project into ``--target``: an empty or new directory unless ``--allow-existing`` (adds missing files
only) is given; a file whose content differs is a conflict and is never replaced without ``--overwrite``; nothing
is deleted. The generated project is then audited with the static engine (no execution). Its tests run only with
``--authorize-execution`` and a trust profile accepted by an executor of the v1.9 trust model (the local executor
provides process hygiene, not a sandbox, and refuses ``untrusted``).

Exit codes: 0 completed; 1 with --strict when the audit reports findings or the tests did not pass;
2 invalid specification, refused generation or invalid arguments.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi_audit.remediate.executors import REGISTRY, TRUST_PROFILES, TRUST_UNTRUSTED, Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ALLOWED_COMMANDS, DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_design import DesignError, DesignOptions, GenerationError, SpecError, render_markdown, render_text, run_design, to_json  # noqa: E402
from fastapi_design.pipeline import TOOL_NAME  # noqa: E402

PROG = "design_fastapi"


def _csv(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=f"{TOOL_NAME}: validate an architecture contract, recommend a structure, generate the project and verify it.")
    parser.add_argument("spec", help="architecture contract (JSON, schema fastapi-architect/spec/1.0)")
    parser.add_argument("--target", metavar="DIR", help="directory that receives the generated project (created when missing; must be empty unless --allow-existing)")
    parser.add_argument("--validate-only", action="store_true", help="validate the specification and print the recommendation; generate nothing")
    parser.add_argument("--allow-existing", action="store_true", help="accept a non-empty target: files that do not exist are added, identical files are kept, differing files are conflicts")
    parser.add_argument("--overwrite", action="store_true", help="replace files whose content differs from the generated one (explicit authorization; nothing is deleted)")
    parser.add_argument("--dry-run", action="store_true", help="compute what would be written without touching the target")
    parser.add_argument("--no-audit", action="store_true", help="skip the static audit of the generated project")
    parser.add_argument("--authorize-execution", action="store_true", help="allow the generated tests to run through an executor of the trust model (requires --trust fixture|trusted and --test-command)")
    parser.add_argument("--trust", choices=TRUST_PROFILES, default=TRUST_UNTRUSTED, help="trust profile declared for the generated project (default untrusted: nothing runs locally)")
    parser.add_argument("--trust-scope", metavar="DIR", help="directory the authorization covers (must contain the target); defaults to the target when --trust is not untrusted")
    parser.add_argument("--executor", default="local", help=f"executor to use (registered: {', '.join(sorted(REGISTRY))})")
    parser.add_argument("--test-command", metavar="CMD", help="command that runs the generated tests inside the target (no shell; python/pytest/uv unless --allow-command)")
    parser.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS")
    parser.add_argument("--env-passthrough", action="append", default=[], metavar="NAMES", help=f"environment variables handed to the command (default: {', '.join(DEFAULT_ENV_PASSTHROUGH)})")
    parser.add_argument("--allow-command", action="append", default=[], metavar="NAME", help=f"additional program names allowed to run (default: {', '.join(DEFAULT_ALLOWED_COMMANDS)})")
    parser.add_argument("--output", metavar="FILE", help="write the JSON architecture report to FILE")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--strict", action="store_true", help="exit 1 when the audit of the generated project reports findings or the tests did not pass")
    args = parser.parse_args(argv)

    if not args.validate_only and not args.target:
        sys.stderr.write(f"{PROG}: error: --target DIR is required unless --validate-only is given\n")
        return 2
    if args.test_command and not args.authorize_execution:
        sys.stderr.write(f"{PROG}: error: --test-command requires --authorize-execution (nothing is executed without it)\n")
        return 2
    if args.authorize_execution and not args.test_command:
        sys.stderr.write(f"{PROG}: error: --authorize-execution needs --test-command CMD\n")
        return 2
    if args.authorize_execution and not args.target:
        sys.stderr.write(f"{PROG}: error: --authorize-execution needs --target DIR\n")
        return 2
    scope = args.trust_scope
    if scope is None and args.authorize_execution and args.trust != TRUST_UNTRUSTED:
        scope = args.target
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
    options = DesignOptions(
        target=args.target,
        allow_existing=bool(args.allow_existing),
        overwrite=bool(args.overwrite),
        dry_run=bool(args.dry_run),
        validate_only=bool(args.validate_only),
        audit=not args.no_audit,
        authorization=authorization,
        policy=policy,
        executor=args.executor,
        test_command=args.test_command,
    )
    try:
        report = run_design(args.spec, options)
    except SpecError as exc:
        sys.stderr.write(f"{PROG}: invalid specification {exc.source}:\n")
        for error in exc.errors:
            sys.stderr.write(f"  - {error}\n")
        return 2
    except (DesignError, GenerationError, FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 2
    payload = to_json(report)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            sys.stderr.write(f"{PROG}: error: cannot write {args.output}: {exc}\n")
            return 2
    if args.format == "json":
        sys.stdout.write(payload)
    elif args.format == "markdown":
        sys.stdout.write(render_markdown(report))
    else:
        sys.stdout.write(render_text(report))
    if args.strict:
        verification = report.get("verification") or {}
        audit = verification.get("audit")
        tests = verification.get("tests")
        if audit and audit["findings_total"]:
            return 1
        if tests is not None and not (tests.get("ran") and tests["run"]["outcome"] == "passed"):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
