"""Authorization and multi-tenancy review signals.

These rules point reviewers at places where a membership or authentication
check must exist; they never conclude that one is missing, because protection
can live in a router, the application, middleware, a gateway or a composed
dependency. Each finding says what to verify and with which negative test.
"""

from __future__ import annotations

import ast
import re
from typing import List, Optional, Set, Tuple

from ..facts import AUTH_RE
from ..model import ENV_MIGRATION, Finding, new_finding
from ..resolver import FunctionInfo, annotation_to_dotted, dotted_name
from . import FileContext

CATEGORY = "security"

TENANT_PARAMS = frozenset({"tenant_id", "org_id", "organization_id", "workspace_id", "company_id"})
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


def _alias_target_is_auth(ctx: FileContext, dotted: Optional[str]) -> bool:
    """True when ``dotted`` names an Annotated alias whose Depends target looks like auth."""
    if not dotted or ctx.facts is None:
        return False
    aliases = ctx.facts.dependency_aliases
    target = aliases.get(dotted)
    if target is None:
        # re-exported through a facade (public.py) or imported with/without a source-root prefix:
        # fall back to the alias short name when it is unique in the project
        short = "." + dotted.rsplit(".", 1)[-1]
        hits = {v for k, v in aliases.items() if k.endswith(short)}
        if len(hits) == 1:
            target = next(iter(hits))
    return bool(target and AUTH_RE.search(target.rsplit(".", 1)[-1]))


CHAIN_DEPTH = 3
TENANT_BINDING_RE = re.compile(r"member|tenant|org|workspace|company|scope|authorize|authorise|permission|policy|access|can_|allowed|guard", re.I)
ADMIN_RE = re.compile(r"admin|owner|superuser|role|perm|policy|authoriz|authoris|rbac|acl|manager|staff|privilege|can_|allowed|scope|guard|require_|grant", re.I)
PRIVILEGE_ATTRS = frozenset({"role", "roles", "is_admin", "is_superuser", "is_staff", "permissions", "scopes", "privileges", "groups", "is_owner"})
PRIVILEGE_CALL_RE = re.compile(
    r"^((grant|revoke)_?(access|role|roles|permission|permissions|admin|privilege|privileges|scope|scopes)"
    r"|(promote|demote|elevate)_(to_)?(role|user|member|admin|owner|privilege|privileges|permissions)"
    r"|set_role|assign_role|add_role|remove_role|change_role|update_role|add_permission|remove_permission|set_permissions"
    r"|make_admin|make_owner|transfer_ownership)",
    re.I,
)


def _graph(ctx: FileContext):
    return getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None


def _dependency_record(ctx: FileContext, dotted: Optional[str], raw_parts: Optional[List[str]] = None):
    """FunctionRecord of a Depends() target named from this file (imported, local, or through an alias)."""
    graph = _graph(ctx)
    if graph is None:
        return None
    module = ctx.facts.file_module.get(ctx.relpath, "") if ctx.facts is not None else ""
    candidates = [dotted]
    if raw_parts:
        candidates.append(f"{module}.{'.'.join(raw_parts)}" if module else ".".join(raw_parts))
    for cand in candidates:
        key = graph.canonical_function(cand) if cand else None
        if key:
            return graph.functions.get(key)
    return None


def _dependency_chain(ctx: FileContext, record, depth: int = 0, seen: Optional[Set[str]] = None) -> List[object]:
    """The dependency's record followed by the records of its own Depends() targets (bounded)."""
    if record is None:
        return []
    seen = seen or set()
    if record.key in seen or depth > CHAIN_DEPTH:
        return []
    seen.add(record.key)
    chain = [record]
    for _param, dotted in record.depends.items():
        chain.extend(_dependency_chain(ctx, _dependency_record(ctx, dotted), depth + 1, seen))
    return chain


def _chain_is_auth(ctx: FileContext, dotted: Optional[str], raw_parts: Optional[List[str]] = None) -> bool:
    """True when a dependency, or any dependency it depends on, looks like authentication/authorization
    (get_context -> get_current_user). This is how auth is reached through abstractions."""
    for rec in _dependency_chain(ctx, _dependency_record(ctx, dotted, raw_parts)):
        if AUTH_RE.search(rec.name) or any(AUTH_RE.search(p) for p in rec.params if p not in TENANT_PARAMS):
            return True
    return False


def _param_signal(ctx: FileContext, func: FunctionInfo) -> bool:
    defaults = func.param_defaults()
    module = ctx.facts.file_module.get(ctx.relpath, "") if ctx.facts is not None else ""
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
                    if inner and _chain_is_auth(ctx, func.resolve(inner), inner):
                        return True  # v1.6: authentication reached through a composed dependency
                elif AUTH_RE.search(arg.arg):
                    return True
        if arg.annotation is not None:
            dotted = annotation_to_dotted(arg.annotation, func.resolve)
            if dotted and AUTH_RE.search(dotted.rsplit(".", 1)[-1]):
                return True
            # Annotated alias defined in this or another module: X = Annotated[T, Depends(require_member)]
            raw_parts = dotted_name(arg.annotation) if isinstance(arg.annotation, (ast.Name, ast.Attribute)) else None
            if raw_parts:
                imported = func.resolve(raw_parts)
                local = f"{module}.{'.'.join(raw_parts)}" if module else ".".join(raw_parts)
                if _alias_target_is_auth(ctx, imported) or _alias_target_is_auth(ctx, local):
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


def _providers(ctx: FileContext, func: FunctionInfo) -> List[Tuple[str, Optional[str], Optional[List[str]], str]]:
    """(param, dotted provider, raw parts, label) for every Depends()/Security() parameter of a route,
    including Annotated aliases resolved through the project's alias registry."""
    out: List[Tuple[str, Optional[str], Optional[List[str]], str]] = []
    defaults = func.param_defaults()
    module = ctx.facts.file_module.get(ctx.relpath, "") if ctx.facts is not None else ""
    for arg in func.params():
        node = defaults.get(arg.arg)
        found = None
        for candidate in (node, arg.annotation):
            if candidate is None:
                continue
            for sub in ast.walk(candidate):
                if isinstance(sub, ast.Call):
                    parts = dotted_name(sub.func)
                    if parts and parts[-1] in {"Depends", "Security"} and sub.args:
                        arg0 = sub.args[0]
                        inner = dotted_name(arg0)
                        label = inner[-1] if inner else ""
                        if inner is None and isinstance(arg0, ast.Call):  # Depends(require(p.X)): factory + its arguments
                            inner = dotted_name(arg0.func)
                            label = ast.unparse(arg0) if hasattr(ast, "unparse") else (inner[-1] if inner else "")
                        found = (func.resolve(inner) if inner else None, inner, label)
                        break
            if found:
                break
        if found is None and arg.annotation is not None and ctx.facts is not None:
            raw = dotted_name(arg.annotation) if isinstance(arg.annotation, (ast.Name, ast.Attribute)) else None
            if raw:
                aliases = ctx.facts.dependency_aliases
                for alias in (func.resolve(raw), f"{module}.{'.'.join(raw)}" if module else ".".join(raw)):
                    target = aliases.get(alias or "")
                    if target is None and alias:
                        short = "." + alias.rsplit(".", 1)[-1]
                        hits = {v for k, v in aliases.items() if k.endswith(short)}
                        target = next(iter(hits)) if len(hits) == 1 else None
                    if target:
                        labels = getattr(ctx.facts, "dependency_alias_labels", {})
                        label = labels.get(alias or "") or next((v for k, v in labels.items() if alias and k.endswith("." + alias.rsplit(".", 1)[-1])), None) or target.rsplit(".", 1)[-1]
                        found = (target if "." in target else None, [target.rsplit(".", 1)[-1]], label)
                        break
        if found:
            out.append((arg.arg, found[0], found[1], found[2]))
    return out


def _names_and_roots(node: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
    return out


def _tenant_binding(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    """Authenticated route that uses a request-supplied tenant id without a visible binding to the principal."""
    route = func.route
    if route is None:
        return
    used = _used_names(func)
    tenant_params = sorted(p.arg for p in func.params() if p.arg in TENANT_PARAMS and p.arg in used)
    if not tenant_params:
        return
    providers = _providers(ctx, func)
    principal_names: Set[str] = set()
    for param, dotted, raw, label in providers:
        chain = _dependency_chain(ctx, _dependency_record(ctx, dotted, raw))
        if TENANT_BINDING_RE.search(label) or TENANT_BINDING_RE.search(param):
            return  # a membership/authorization dependency binds the tenant
        for rec in chain:
            if rec.tenant_params or TENANT_BINDING_RE.search(rec.name) or any(p in TENANT_PARAMS for p in rec.params):
                return  # the dependency itself receives the tenant: the binding happens there
        if AUTH_RE.search(label) or any(AUTH_RE.search(rec.name) for rec in chain) or AUTH_RE.search(param):
            principal_names.add(param)
    for arg in func.params():
        if arg.arg not in tenant_params and AUTH_RE.search(arg.arg) and arg.arg not in TENANT_PARAMS:
            principal_names.add(arg.arg)
    if not principal_names:
        return  # unauthenticated: SEC_TENANT_ID_FROM_REQUEST covers it
    if route.decorator is not None:
        for kw in route.decorator.keywords:
            if kw.arg == "dependencies" and TENANT_BINDING_RE.search(ast.unparse(kw.value) if hasattr(ast, "unparse") else ""):
                return
    tenant_set = set(tenant_params)
    for node in func.scope_nodes():
        if isinstance(node, (ast.Compare, ast.BoolOp, ast.Assert)) or (isinstance(node, ast.If)):
            names = _names_and_roots(node.test if isinstance(node, ast.If) else node)
            if names & tenant_set and names & principal_names:
                return  # tenant compared against the principal's data
        if isinstance(node, ast.Call):
            names = set()
            for a in [*node.args, *[k.value for k in node.keywords]]:
                names |= _names_and_roots(a)
            if names & tenant_set and names & principal_names:
                return  # tenant and principal flow together into the same call
    where = f"'{func.qualname}' ({route.method} {route.path or ''})".replace(" )", ")")
    out.append(
        new_finding(
            "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL",
            ctx.relpath,
            func.lineno,
            getattr(func.node, "col_offset", 0),
            (
                f"{where} is authenticated through '{', '.join(sorted(principal_names))}' and uses '{', '.join(tenant_params)}' "
                "from the request, but no visible step binds the two: the authentication dependency chain takes no tenant "
                "parameter, and the principal never flows into the calls that receive the tenant id. If membership is "
                "enforced elsewhere (middleware, row-level security keyed by the principal, a service that checks it) this is "
                "a false positive; otherwise any authenticated caller can address any tenant. Not verifiable statically."
            ),
            confidence="low",
            symbol=tenant_params[0],
            function=func.qualname,
            environment=ctx.environment,
            evidence="unknown",
            recommended_test=CROSS_TENANT_TEST,
        )
    )


def _privilege_change(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    route = func.route
    if route is None:
        return
    change: Optional[Tuple[int, str]] = None
    for node in func.scope_nodes():
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr in PRIVILEGE_ATTRS:
                    change = (node.lineno, f"{ast.unparse(t) if hasattr(ast, 'unparse') else t.attr} = ...")
                    break
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if name and PRIVILEGE_CALL_RE.search(name):
                change = (node.lineno, f"{name}()")
            elif any(kw.arg in PRIVILEGE_ATTRS for kw in node.keywords) and name in {"update", "values", "update_user", "save"}:
                change = (node.lineno, f"{name}({[kw.arg for kw in node.keywords if kw.arg in PRIVILEGE_ATTRS][0]}=...)")
        if change:
            break
    if change is None:
        return
    # an admin/owner/role-level signal anywhere on the handler, its dependency chain or its decorator
    for param, dotted, raw, label in _providers(ctx, func):
        if ADMIN_RE.search(label) or ADMIN_RE.search(param):
            return
        for rec in _dependency_chain(ctx, _dependency_record(ctx, dotted, raw)):
            if ADMIN_RE.search(rec.name):
                return
    for arg in func.params():
        if ADMIN_RE.search(arg.arg) and arg.arg not in TENANT_PARAMS:
            return
        if arg.annotation is not None and ADMIN_RE.search(ast.unparse(arg.annotation) if hasattr(ast, "unparse") else ""):
            return
    if route.decorator is not None:
        for kw in route.decorator.keywords:
            if kw.arg == "dependencies" and ADMIN_RE.search(ast.unparse(kw.value) if hasattr(ast, "unparse") else ""):
                return
    for node in func.scope_nodes():  # an explicit check in the body: if not user.is_admin / require(...)
        if isinstance(node, ast.If) and ADMIN_RE.search(ast.unparse(node.test) if hasattr(ast, "unparse") else ""):
            return
    has_auth = _param_signal(ctx, func) or _router_signal(ctx, func)
    where = f"'{func.qualname}' ({route.method} {route.path or ''})".replace(" )", ")")
    out.append(
        new_finding(
            "SEC_PRIVILEGE_CHANGE_UNVERIFIED",
            ctx.relpath,
            func.lineno,
            getattr(func.node, "col_offset", 0),
            (
                f"{where} changes privileges ({change[1]} at line {change[0]}) and "
                + ("only a generic authentication signal is visible" if has_auth else "no authentication signal is visible")
                + ": no dependency, parameter, decorator or body check names an admin/owner/role/permission policy. Whether "
                "the caller may change privileges cannot be decided statically; if a policy is applied elsewhere this is a false positive."
            ),
            confidence="low" if (has_auth and _router_signal(ctx, func)) else "medium",
            symbol=change[1],
            function=func.qualname,
            environment=ctx.environment,
            evidence="unknown",
            recommended_test="As a plain member (not admin/owner) call this route to change another user's role or flags; expect 403 and no change. Repeat as an admin to keep the positive path.",
        )
    )


def _route_rules(ctx: FileContext, out: List[Finding]) -> None:
    for func in ctx.functions:
        route = func.route
        if route is None:
            continue
        if _param_signal(ctx, func) or _router_signal(ctx, func):
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
        for func in ctx.functions:
            if func.route is not None:
                _tenant_binding(ctx, func, out)
                _privilege_change(ctx, func, out)
    _rls_rules(ctx, out)
    return out
