#!/usr/bin/env python3
"""Discover an existing FastAPI project and evolve a generated one under an evolution contract (v2.0).

    python3 evolve_fastapi.py discover PROJECT [--format text|json] [--output FILE]
    python3 evolve_fastapi.py propose PROJECT                  # candidate modules of a hand-written project (nothing adopted)
    python3 evolve_fastapi.py adopt ADOPTION.json              # record the adoption baseline (adoption/1.0)
    python3 evolve_fastapi.py recover PROJECT [--forward]      # finish or undo an interrupted operation
    python3 evolve_fastapi.py plan CONTRACT.json [--baseline-contract SPEC.json] [--no-verify] [--format ...] [--output FILE]
    python3 evolve_fastapi.py apply CONTRACT.json [--baseline-contract SPEC.json]
                              [--authorize-execution --trust fixture|trusted [--trust-scope DIR] --test-command CMD
                               --timeout N --env-passthrough NAMES --allow-command NAME --executor local]
                              [--format text|json] [--output FILE] [--strict]

``discover`` is static: it parses the project (never imports it) and reports layout, routers, routes, models,
services, repositories, dependencies, settings, lifespan, Alembic chain, declared contracts and the ownership of
every file (managed by the generator and unchanged, managed but modified by hand, unmanaged, or unknown when there
is no manifest). ``plan`` computes the change set (files to create, integration points to regenerate, preserved
manual changes, conflicts, preconditions, public-contract changes, the additive migration when PostgreSQL is used,
risks, required tests) and, unless ``--no-verify``, applies it to a copy and audits the result; nothing in the
project is written. ``apply`` does the same and then writes the project only when the verification in the copy did
not reject the plan (new high/critical findings, changed existing routes, files touched outside the plan, failing
tests). Tests run only with ``--authorize-execution`` and a trust profile the executor accepts (the local executor
is process hygiene, not a sandbox); without tests the best state is ``audit_clean``, never ``verified``.

Exit codes: 0 completed (planned / audit_clean / verified; with ``apply``, written); 1 blocked, rejected,
inconclusive or refused; 2 invalid contract or arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi_audit.remediate.executors import REGISTRY, TRUST_PROFILES, TRUST_UNTRUSTED, Authorization  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_ALLOWED_COMMANDS, DEFAULT_ENV_PASSTHROUGH, ExecutionPolicy  # noqa: E402
from fastapi_design.adoption import AdoptionError, adopt, load_adoption_contract, propose_adoption, render_proposal  # noqa: E402
from fastapi_design.discovery import discover  # noqa: E402
from fastapi_design.evolution import EvolutionError, EvolveOptions, render_text, run_evolution, to_json  # noqa: E402
from fastapi_design.journal import load_operation, pending_operations, recover, unreadable_operations  # noqa: E402

PROG = "evolve_fastapi"
OK_STATES = ("planned", "noop", "audit_clean", "verified")


def _csv(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def _render_discovery(model: dict) -> str:
    lines = [f"project: {model['root']}", f"package: {model['package']} (source root {model['source_root'] or '.'}); layout: {model['layout']} ({'; '.join(model['layout_evidence'])}); modules: {', '.join(model['modules']) or '-'}"]
    gen = model.get("generated")
    lines.append(f"generated: {gen['schema']} by {gen['tool']['name']} {gen['tool']['version']}, style {gen['style']}, {gen['files']} managed files, baseline contract {'embedded' if gen['has_spec_document'] else 'missing'}, history {len(gen['history'])} operation(s)" if gen else "generated: no manifest (hand-written or unknown origin)")
    lines.append("ownership: " + ", ".join(f"{k} {v}" for k, v in model["ownership_counts"].items()))
    lines.append(f"routers: {len(model['routers'])}; routes: {len(model['routes'])}; pydantic models: {len(model['pydantic_models'])}; settings: {len(model['settings'])}; sqlalchemy models: {len(model['sqlalchemy_models'])}; services: {len(model['services'])}; repositories: {len(model['repositories'])}; dependency aliases: {len(model['dependencies'])}; lifespan: {model['lifespan']['function'] if model.get('lifespan') else '-'}")
    for r in model["routes"]:
        lines.append(f"  {r['method']:6} {', '.join(r['effective_paths']) or r['path']}  ({r['function']}, {r['path_resolution']}, auth {r['authentication']})")
    al = model.get("alembic")
    if al:
        lines.append(f"alembic: {len(al['revisions'])} revision(s), heads {al['heads']}, chain ok {al['chain_ok']}, historical modified {al['historical_modified']}")
    if model.get("declared_contracts") is not None:
        lines.append("declared contracts: .audit-fastapi.json present")
    if model.get("audit_summary"):
        lines.append(f"audit: {model['audit_summary']['findings_total']} active finding(s) {model['audit_summary']['by_rule']}")
    for u in model["uncertainties"]:
        lines.append(f"  ? {u}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="Discover an existing FastAPI project and evolve a generated one under a contract.")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="static model of an existing project")
    d.add_argument("project")
    d.add_argument("--no-audit", action="store_true", help="skip the route contracts and audit summary")
    pr = sub.add_parser("propose", help="candidate modules of a hand-written project for adoption (nothing is adopted)")
    pr.add_argument("project")
    ad = sub.add_parser("adopt", help="record the adoption baseline of the modules named in an adoption contract")
    ad.add_argument("contract", help="adoption contract (JSON, schema fastapi-architect/adoption/1.0)")
    rc = sub.add_parser("recover", help="finish or undo an interrupted operation (journal under .fastapi-architect/ops)")
    rc.add_argument("project")
    rc.add_argument("--forward", action="store_true", help="publish the pending files (default: roll back the published ones)")
    rc.add_argument("--op", metavar="ID", help="only this operation id")
    for name in ("plan", "apply"):
        p = sub.add_parser(name, help="compute the change set (and verify it in a copy)" if name == "plan" else "verify in a copy, then write the project")
        p.add_argument("contract", help="evolution contract (JSON, schema fastapi-architect/evolution/1.0)")
        p.add_argument("--baseline-contract", metavar="SPEC", help="the project's original contract when its manifest predates 1.1")
        p.add_argument("--no-verify", action="store_true", help="plan only: do not apply to a copy" if name == "plan" else argparse.SUPPRESS)
        p.add_argument("--authorize-execution", action="store_true", help="allow the tests of the evolved copy to run through an executor of the trust model")
        p.add_argument("--trust", choices=TRUST_PROFILES, default=TRUST_UNTRUSTED)
        p.add_argument("--trust-scope", metavar="DIR")
        p.add_argument("--executor", default="local", help=f"registered: {', '.join(sorted(REGISTRY))}")
        p.add_argument("--test-command", metavar="CMD")
        p.add_argument("--timeout", type=float, default=600.0)
        p.add_argument("--env-passthrough", action="append", default=[], metavar="NAMES")
        p.add_argument("--allow-command", action="append", default=[], metavar="NAME")
        p.add_argument("--keep-copy", action="store_true", help="keep the verification copy (its path is reported)")
    for p in (d, pr, ad, rc, sub.choices["plan"], sub.choices["apply"]):
        p.add_argument("--output", metavar="FILE", help="write the JSON report to FILE")
        p.add_argument("--format", choices=("text", "json"), default="text")
    sub.choices["plan"].add_argument("--strict", action="store_true", help=argparse.SUPPRESS)
    sub.choices["apply"].add_argument("--strict", action="store_true", help="exit 1 unless the verification ended verified")
    args = parser.parse_args(argv)

    if args.command == "discover":
        try:
            model = discover(args.project, audit=not args.no_audit).to_dict()
        except FileNotFoundError as exc:
            sys.stderr.write(f"{PROG}: error: {exc}\n")
            return 2
        payload = json.dumps(model, indent=2, sort_keys=True) + "\n"
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        sys.stdout.write(payload if args.format == "json" else _render_discovery(model))
        return 0

    if args.command in ("propose", "adopt", "recover"):
        try:
            if args.command == "propose":
                payload_obj = propose_adoption(discover(args.project, audit=True))
                text = render_proposal(payload_obj)
            elif args.command == "adopt":
                payload_obj = adopt(load_adoption_contract(args.contract))
                text = f"adopted {', '.join(payload_obj['adopted'])} in {payload_obj['project']}: {payload_obj['written']} written (operation {payload_obj['operation']}); {payload_obj['protected_files']} other file(s) stay protected\n" + "".join(f"  ? {u}\n" for u in payload_obj["uncertainties"])
            else:
                ops = pending_operations(os.path.abspath(args.project))
                if args.op:
                    ops = [op for op in ops if op.op_id == args.op]
                reports = [recover(os.path.abspath(args.project), op, strategy="forward" if args.forward else "rollback").to_dict() for op in ops]
                unreadable = [u for u in unreadable_operations(os.path.abspath(args.project)) if not args.op or u["op_id"] == args.op]
                payload_obj = {"project": os.path.abspath(args.project), "recovered": reports, "unreadable": unreadable}
                text = ("no pending operation\n" if not reports and not unreadable else "".join(f"operation {r['op_id']} ({r['strategy']}): {r['state']}; restored {len(r['restored'])}, published {len(r['published'])}, already done {len(r['already_done'])}, conflicts {len(r['conflicts'])}\n" + "".join(f"  ! {k}: {v}\n" for k, v in r["conflicts"].items()) for r in reports))
                text += "".join(f"operation {u['op_id']}: needs_attention; {u['reason']} ({u['staged_files']} staged file(s) kept under {os.path.dirname(u['path'])}); inspect by hand, nothing was touched\n" for u in unreadable)
        except (AdoptionError,) as exc:
            sys.stderr.write(f"{PROG}: adoption refused ({exc.source}):\n")
            for error in exc.errors:
                sys.stderr.write(f"  - {error}\n")
            return 2
        except (FileNotFoundError, ValueError, OSError) as exc:
            sys.stderr.write(f"{PROG}: error: {exc}\n")
            return 2
        payload = json.dumps(payload_obj, indent=2, sort_keys=True) + "\n"
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(payload)
        sys.stdout.write(payload if args.format == "json" else text)
        if args.command == "recover" and (any(r["state"] == "needs_attention" for r in payload_obj["recovered"]) or payload_obj.get("unreadable")):
            return 1
        return 0

    if args.test_command and not args.authorize_execution:
        sys.stderr.write(f"{PROG}: error: --test-command requires --authorize-execution (nothing is executed without it)\n")
        return 2
    if args.authorize_execution and not args.test_command:
        sys.stderr.write(f"{PROG}: error: --authorize-execution needs --test-command CMD\n")
        return 2
    scope = args.trust_scope
    if scope is None and args.authorize_execution and args.trust != TRUST_UNTRUSTED:
        try:
            with open(args.contract, "r", encoding="utf-8") as handle:
                project_path = json.load(handle).get("project", {}).get("path")
            if isinstance(project_path, str):
                scope = project_path if os.path.isabs(project_path) else os.path.join(os.path.dirname(os.path.abspath(args.contract)), project_path)
        except (OSError, ValueError, AttributeError):
            scope = None
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
    options = EvolveOptions(
        apply=args.command == "apply",
        verify=not getattr(args, "no_verify", False),
        authorization=authorization,
        policy=policy,
        executor=args.executor,
        test_command=args.test_command,
        baseline_contract=args.baseline_contract,
        keep_copy=bool(args.keep_copy),
    )
    try:
        report = run_evolution(args.contract, options)
    except EvolutionError as exc:
        sys.stderr.write(f"{PROG}: invalid contract {exc.source}:\n")
        for error in exc.errors:
            sys.stderr.write(f"  - {error}\n")
        return 2
    except (FileNotFoundError, ValueError) as exc:
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
    sys.stdout.write(payload if args.format == "json" else render_text(report))
    if report["state"] not in OK_STATES:
        return 1
    if args.command == "apply" and report["applied"].get("state") not in ("applied", "nothing_to_apply"):
        return 1
    if getattr(args, "strict", False) and report["state"] != "verified":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
