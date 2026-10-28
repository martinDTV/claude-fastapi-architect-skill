"""Authorization and multi-tenancy review signals.

These rules point reviewers at places where a membership or authentication
check must exist; they never conclude that one is missing, because protection
can live in a router, the application, middleware, a gateway or a composed
dependency. Each finding says what to verify and with which negative test.
"""

from __future__ import annotations

import ast
import re
from typing import List, Optional, Set

from ..facts import AUTH_RE
from ..model import ENV_MIGRATION, Finding, new_finding
from ..resolver import FunctionInfo, annotation_to_dotted, dotted_name
from . import FileContext

CATEGORY = "security"

TENANT_PARAMS = frozenset({"tenant_id", "org_id", "organization_id", "workspace_id", "account_id", "company_id"})
PUBLIC_PATH_RE = re.compile(
    r"^/?(|health|healthz|ready|readyz|live|livez|metrics|ping|version|docs|openapi\.json|redoc|status)/?$"
)
# Session-level settings that survive the transaction on a pooled connection.
SET_GUC_RE = re.compile(r"^\s*SET\s+(?!LOCAL\b)(?:SESSION\s+)?([A-Za-z_][\w]*\.[\w.]+)\s*(?:=|TO\b)", re.I)
SET_ROLE_RE = re.compile(r"^\s*SET\s+(?!LOCAL\b)(?:SESSION\s+)?ROLE\b", re.I)
SET_CONFIG_FALSE_RE = re.compile(r"set_config\(\s*'[^']+'\s*,[^,]+,\s*false\s*\)", re.I)
CROSS_TENANT_TEST = (
    "Authenticate as a member of tenant A and request tenant B's identifier through this "
    "route (single resource, list, search, aggregate); expect 403/404 and no row. Repeat "
    "with a valid identifier of A to prove the positive path still works."
)


def _param_signal(func: FunctionInfo) -> bool:
    defaults = func.param_defaults()
    for arg in func.params():
        default = defaults.get(arg.arg)
        if isinstance(default, ast.Call):
            parts = dotted_name(default.func)
            if parts and parts[-1] in {"Depends", "Security"}:
                if default.args:
                    inner = dotted_name(default.args[0])
                    label = inner[-1] if inner else ast.unparse(default.args[0]) if hasattr(ast, "unparse") else ""
                    if AUTH_RE.search(label):
                        return True
                elif AUTH_RE.search(arg.arg):
                    return True
        if arg.annotation is not None:
            dotted = annotation_to_dotted(arg.annotation, func.resolve)
            if dotted and AUTH_RE.search(dotted.rsplit(".", 1)[-1]):
                return True
            text = ast.unparse(arg.annotation) if hasattr(ast, "unparse") else ""
            if text and ("Depends(" in text or "Security(" in text) and AUTH_RE.search(text):
                return True
        if AUTH_RE.search(arg.arg) and arg.arg not in TENANT_PARAMS:
            return True
    return False


def _router_signal(ctx: FileContext, func: FunctionInfo) -> bool:
    route = func.route
    if route is None:
        return False
    if route.has_dependencies:
        return True
    receiver_root = route.receiver.split(".")[0]
    for stmt in ctx.tree.body:  # local `router = APIRouter(dependencies=[...])`
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            if any(isinstance(t, ast.Name) and t.id == receiver_root for t in stmt.targets):
                if any(kw.arg == "dependencies" for kw in stmt.value.keywords):
                    return True
    if ctx.facts is not None:
        dotted = func.resolve([receiver_root])
        if dotted and dotted in ctx.facts.protected_routers:
            return True
        module = ctx.facts.file_module.get(ctx.relpath, "")
        if f"{module}.{receiver_root}" in ctx.facts.protected_routers:
            return True
        if ctx.facts.global_auth_signal:
            return True
    return False


def _used_names(func: FunctionInfo) -> Set[str]:
    return {n.id for n in ast.walk(func.node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _route_rules(ctx: FileContext, out: List[Finding]) -> None:
    for func in ctx.functions:
        route = func.route
        if route is None:
            continue
        if _param_signal(func) or _router_signal(ctx, func):
            continue
        path = route.path or ""
        used = _used_names(func)
        tenant_params = sorted(p.arg for p in func.params() if p.arg in TENANT_PARAMS and p.arg in used)
        where = f"'{func.qualname}' ({route.method} {path})".replace(" )", ")")
        if tenant_params:
            out.append(
                new_finding(
                    "SEC_TENANT_ID_FROM_REQUEST",
                    ctx.relpath,
                    func.lineno,
                    getattr(func.node, "col_offset", 0),
                    (
                        f"{where} receives '{', '.join(tenant_params)}' from the request and uses it, and no "
                        "authentication or membership dependency is visible on the handler, its router or the "
                        "application. If a check exists in middleware, a gateway or a composed dependency this is "
                        "a false positive; otherwise any caller can address any tenant. Not verifiable statically: "
                        "confirm with a cross-tenant negative test."
                    ),
                    confidence="medium",
                    symbol=tenant_params[0],
                    function=func.qualname,
                    environment=ctx.environment,
                    recommended_test=CROSS_TENANT_TEST,
                )
            )
            continue
        if PUBLIC_PATH_RE.match(path):
            continue
        out.append(
            new_finding(
                "SEC_ROUTE_WITHOUT_AUTH_SIGNAL",
                ctx.relpath,
                func.lineno,
                getattr(func.node, "col_offset", 0),
                (
                    f"{where}: no authentication/authorization dependency is visible on the handler, its "
                    "router or the application in the analysed files. Protection may be applied elsewhere "
                    "(middleware, gateway, composed dependency) or the route may be intentionally public. "
                    "Informational; not verifiable statically."
                ),
                confidence="low",
                symbol=route.receiver,
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Call the route without credentials and with another principal's credentials; assert the documented policy (401/403 or public).",
            )
        )


def _rls_rules(ctx: FileContext, out: List[Finding]) -> None:
    seen: Set[int] = set()
    for node in ast.walk(ctx.tree):
        text: Optional[str] = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if not text or "set" not in text.lower() or node.lineno in seen:
            continue
        hit: Optional[str] = None
        for line in text.splitlines():
            if SET_GUC_RE.match(line) or SET_ROLE_RE.match(line):
                hit = line.strip()
                break
            if SET_CONFIG_FALSE_RE.search(line):
                hit = line.strip()
                break
        if hit is None:
            continue
        seen.add(node.lineno)
        out.append(
            new_finding(
                "SEC_RLS_SET_NOT_LOCAL",
                ctx.relpath,
                node.lineno,
                node.col_offset,
                (
                    f"Session-level setting in SQL: \"{hit[:80]}\". Without LOCAL (or set_config(..., true)) the "
                    "value persists on the pooled connection after the transaction ends and applies to the next "
                    "request that reuses it; with RLS that means another tenant's context. Use 'SET LOCAL' inside "
                    "the transaction, or set_config(name, value, true), and reset the setting on connection checkout."
                ),
                confidence="high",
                symbol="SET",
                environment=ctx.environment,
                recommended_test=(
                    "On one pooled connection: begin, set the tenant context as the code does, commit, then run "
                    "SHOW <setting> in a new transaction on the same connection; it must be empty/reset."
                ),
            )
        )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment == ENV_MIGRATION:
        return out
    if ctx.fastapi_imported or any(f.route for f in ctx.functions):
        _route_rules(ctx, out)
    _rls_rules(ctx, out)
    return out
