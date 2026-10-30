"""Declared, opt-in architectural contracts (v1.7).

Nothing here fires unless the project declares a contract in ``.audit-fastapi.json`` (plain JSON,
read with ``json.load``; no project Python is ever imported or executed):

    "contracts": {
      "route_auth_required": ["/api/*"],                 # routes under these paths must be authenticated
      "mutation_authorization_required": ["/api/*"],     # POST/PUT/PATCH/DELETE there must show an authorization kind
      "forbidden_imports": [{"from": "app.api", "to": "app.infra"}],
      "commit_layers": ["service"]                       # only files in these layers may commit
    }

Each rule reports the declaration it checks, so a finding is always "the code disagrees with
what the project wrote down", never a judgement about what the architecture should be.
"""

from __future__ import annotations

import ast
import fnmatch
from typing import Any, Dict, List, Optional

from ..contracts import MUTATING
from ..kinds import SESSION_KINDS, SESSION_LIKE_NAMES, function_kinds_ex, receiver_kind, receiver_name
from ..model import ENV_APPLICATION, Finding, new_finding
from . import FileContext
from .async_rules import iter_function_calls

CATEGORY = "contracts"


def _declared(ctx: FileContext) -> Dict[str, Any]:
    if ctx.facts is None:
        return {}
    block = ctx.facts.config.get("contracts")
    return block if isinstance(block, dict) else {}


def _globs(value: Any) -> List[str]:
    """Validated upstream by config_schema.validate_config: a list of '/...' globs."""
    return [str(g) for g in value] if isinstance(value, list) else []


def _matches(paths: List[str], globs: List[str]) -> Optional[str]:
    for g in globs:
        for path in paths:
            if fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path.lstrip("/"), g):
                return g
    return None


def _route_contracts(ctx: FileContext, declared: Dict[str, Any], out: List[Finding]) -> None:
    auth_globs = _globs(declared.get("route_auth_required"))
    authz_globs = _globs(declared.get("mutation_authorization_required"))
    if not auth_globs and not authz_globs:
        return
    for c in getattr(ctx, "contracts", []):
        paths = [c.full_path or (c.path or ""), c.path or ""]
        where = f"'{c.function}' ({c.method} {c.full_path or c.path or ''})".replace(" )", ")")
        g = _matches(paths, auth_globs)
        if g and c.authentication == "absent" and not c.public:
            out.append(
                new_finding(
                    "CONTRACT_ROUTE_AUTH_REQUIRED", ctx.relpath, c.line, 0,
                    f"{where}: the project declares that routes matching '{g}' (contracts.route_auth_required) are authenticated, "
                    "but no authentication dependency, router/application dependency or middleware is visible and the route is not "
                    "declared public (public_routes or '# audit-fastapi: public').",
                    confidence="high", symbol=g, function=c.function, environment=ctx.environment,
                    recommended_test="Call the route without credentials; the contract says it must answer 401/403.",
                )
            )
        g = _matches(paths, authz_globs)
        if g and c.method in MUTATING and c.authentication != "absent" and not c.authorization and not c.public:
            out.append(
                new_finding(
                    "CONTRACT_MUTATION_AUTHZ_REQUIRED", ctx.relpath, c.line, 0,
                    f"{where}: the project declares that mutations matching '{g}' (contracts.mutation_authorization_required) show an "
                    "authorization kind (role, scopes, membership, resource dependency, policy call, principal flowing with the resource), "
                    "and this route shows authentication only.",
                    confidence="high", symbol=g, function=c.function, environment=ctx.environment,
                    recommended_test="As an authenticated user without the right, call the route; the contract says it must answer 403/404.",
                )
            )


def _forbidden_imports(ctx: FileContext, declared: Dict[str, Any], out: List[Finding]) -> None:
    pairs = [p for p in declared.get("forbidden_imports", []) if isinstance(p, dict) and p.get("from") and p.get("to")]
    if not pairs or ctx.facts is None:
        return
    module = ctx.facts.file_module.get(ctx.relpath, "")
    names = {module, *(a for a in _module_aliases(ctx, module))}
    reported_lines = set()
    for dotted, line, _module_level in ctx.facts.imports.get(ctx.relpath, []):
        if line in reported_lines:
            continue  # `from a.b import c` yields two edges (a.b and a.b.c) on one line
        canon = ctx.facts.canonical(dotted) or dotted
        for pair in pairs:
            src, dst = str(pair["from"]), str(pair["to"])
            if any(n == src or n.startswith(src + ".") for n in names) and any(t == dst or t.startswith(dst + ".") for t in (dotted, canon)):
                reported_lines.add(line)
                out.append(
                    new_finding(
                        "CONTRACT_IMPORT_FORBIDDEN", ctx.relpath, line, 0,
                        f"'{module}' imports '{dotted}' at line {line}; the project declares that '{src}' must not import '{dst}' "
                        "(contracts.forbidden_imports). Route the dependency through the allowed layer or revise the declaration.",
                        confidence="high", symbol=dotted, environment=ctx.environment,
                    )
                )
                break


def _module_aliases(ctx: FileContext, module: str) -> List[str]:
    out = []
    for name, rel in ctx.facts.module_map.items():
        if rel == ctx.relpath and name != module:
            out.append(name)
    return out


def _commit_layers(ctx: FileContext, declared: Dict[str, Any], out: List[Finding]) -> None:
    layers = [str(l) for l in declared.get("commit_layers", []) if isinstance(l, str)]
    if not layers or ctx.facts is None or ctx.environment != ENV_APPLICATION:
        return
    layer = ctx.facts.layer_of(ctx.relpath)
    if layer in layers:
        return
    for func in ctx.functions:
        kinds, _ev = function_kinds_ex(func, ctx.facts, ctx.local_factories, ctx.relpath)
        for call in iter_function_calls(func):
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "commit"):
                continue
            name = receiver_name(call)
            if receiver_kind(call, kinds) in SESSION_KINDS or name in SESSION_LIKE_NAMES or (name or "").startswith("self."):
                out.append(
                    new_finding(
                        "CONTRACT_COMMIT_OUTSIDE_LAYER", ctx.relpath, call.lineno, call.col_offset,
                        f"'{name}.commit()' in '{func.qualname}': this file is classified in layer '{layer}' and the project declares "
                        f"that transactions belong to {', '.join(layers)} (contracts.commit_layers). Move the commit to the owning "
                        "use case or revise the layer mapping (`layers` in .audit-fastapi.json).",
                        confidence="high" if receiver_kind(call, kinds) in SESSION_KINDS else "medium", symbol="session.commit",
                        function=func.qualname, environment=ctx.environment,
                    )
                )
                break


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    declared = _declared(ctx)
    if not declared:
        return out
    _route_contracts(ctx, declared, out)
    _forbidden_imports(ctx, declared, out)
    _commit_layers(ctx, declared, out)
    return out
