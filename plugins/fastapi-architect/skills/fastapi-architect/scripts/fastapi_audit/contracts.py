"""Security contracts per route (v1.7): what the source shows about authentication and
authorization, classified conservatively and reported as data, never as a verdict.

A contract says *which kind* of protection is visible on a route: authentication through
``Depends``/``Security`` chains (any depth up to three), router or application dependencies,
middleware; authorization by role, permission or scopes (class-based checkers, permission
factories, ``Security(scopes=...)``, decorators such as ``@requires``), by membership (a
dependency that receives the tenant id with the principal), by resource (a dependency that
receives the resource id, a service call that receives principal and resource together, or a
call to an authorization service), and whether the route is public on purpose (health-style
path, ``public_routes`` globs in ``.audit-fastapi.json``, or an ``# audit-fastapi: public``
marker). "Unknown" is a legitimate outcome: the rules built on contracts only ask questions
about it. Nothing here executes code.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .facts import AUTH_RE
from .resolver import FunctionInfo, dotted_name
from .suppressions import line_comments

PUBLIC_MARKER_RE = re.compile(r"#\s*audit-fastapi:\s*public\b", re.I)
PUBLIC_PATH_RE = re.compile(
    r"^/?(|health|healthz|ready|readyz|live|livez|metrics|ping|version|docs|openapi\.json|redoc|status)/?$"
)
TENANT_PARAMS = frozenset({"tenant_id", "org_id", "organization_id", "workspace_id", "company_id"})


def tenant_param_set(ctx) -> frozenset:
    """Built-in tenant parameter names plus ``tenant_params`` from ``.audit-fastapi.json`` (domain names such as clinic_id)."""
    extra = []
    if ctx is not None and getattr(ctx, "facts", None) is not None:
        extra = [str(x) for x in ctx.facts.config.get("tenant_params", []) if isinstance(x, str)]
    return TENANT_PARAMS | frozenset(extra)
# "staff"/"manager" are user types more often than privileges (current_staff is authentication):
# privilege attributes such as is_staff are matched separately by PRIVILEGE_ATTRS in rules.security
ROLE_RE = re.compile(r"admin|owner|superuser|role|perm|policy|authoriz|authoris|rbac|acl|privilege|can_|allowed|scope|guard|require|grant|needs|checker", re.I)
MEMBERSHIP_RE = re.compile(r"member|tenant|org|workspace|company|clinic|belongs|same_", re.I)
RESOURCE_DEP_RE = re.compile(r"access|owner|own|resource|verify|ensure|check|permit|allow|can_|authoriz|authoris|load_|get_(?!current)", re.I)
AUTHZ_CALL_RE = re.compile(r"^(authorize|authorise|can|check|ensure|assert|permit|allow|verify|require|enforce|guard)", re.I)
DECORATOR_AUTHZ_RE = re.compile(r"requires|require|permission|authoriz|authoris|role|scope|login_required|auth", re.I)
RESOURCE_PARAM_RE = re.compile(r"(^|_)(id|uuid|slug|key)$", re.I)
OPTIONAL_RE = re.compile(r"optional|anonymous|maybe|or_none|_or_guest", re.I)
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def route_bucket(c: "RouteContract") -> str:
    """Disjoint classification used by the summary partition (every route lands in exactly one):
    public_declared (config glob or marker) > unauthenticated routes, split into exposure_undetermined
    (a health-check naming convention suggests public, nothing declares it) and unauthenticated_not_public
    > optional_authentication > authenticated buckets by what relates the caller to the action."""
    if c.public:
        return "public_declared"
    if c.authentication == "absent":
        return "exposure_undetermined" if c.public_source == "path" else "unauthenticated_not_public"
    if c.authentication == "optional":
        return "optional_authentication"
    if c.authorization:
        return "authenticated_authorization_visible"
    if c.resource_params or c.tenant_params:
        return "authenticated_authorization_unknown"
    return "authenticated_only"


@dataclass
class RouteContract:
    file: str
    line: int
    function: str
    method: str
    path: Optional[str]
    authentication: str = "absent"  # present | optional | absent
    authentication_evidence: List[str] = field(default_factory=list)
    authorization: List[str] = field(default_factory=list)  # role | scopes | decorator | membership | resource_dependency | principal_with_resource | authorization_call | tenant_comparison | router_dependencies
    authorization_evidence: List[str] = field(default_factory=list)
    public: bool = False
    public_source: Optional[str] = None  # path | config | marker
    principal_params: List[str] = field(default_factory=list)
    tenant_params: List[str] = field(default_factory=list)
    resource_params: List[str] = field(default_factory=list)
    resource_calls_without_principal: List[str] = field(default_factory=list)  # "callee(line)" receiving a resource id but no principal
    evidence: str = "verified"
    full_path: str = ""  # router prefix (same file) + route path
    principal_kind: str = "unknown"  # v1.8 review: value | none | unknown (see principal_kind_of)
    effective_paths: List[str] = field(default_factory=list)  # v2.0: paths composed through include_router from the application(s)
    path_resolution: str = "unmounted"  # v2.0: resolved | ambiguous (several mounts) | unresolved (a prefix is not a constant) | unmounted
    named_only: List[str] = field(default_factory=list)  # v2.0 hardening: dependencies named like a policy whose check is not visible (never an authorization kind)

    def to_dict(self) -> Dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "method": self.method,
            "path": self.path,
            "full_path": self.full_path,
            "bucket": route_bucket(self),
            "authentication": self.authentication,
            "authentication_evidence": list(self.authentication_evidence),
            "authorization": list(self.authorization),
            "authorization_evidence": list(self.authorization_evidence),
            "public": self.public,
            "public_source": self.public_source,
            "principal_params": list(self.principal_params),
            "tenant_params": list(self.tenant_params),
            "resource_params": list(self.resource_params),
            "evidence": self.evidence,
            "principal_kind": self.principal_kind,  # schema 1.6 (review)
            "effective_paths": list(self.effective_paths),  # v2.0
            "path_resolution": self.path_resolution,  # v2.0
            "authorization_named_only": list(self.named_only),  # v2.0 hardening
        }


# --------------------------------------------------------------------------- #
# helpers shared with rules.security
# --------------------------------------------------------------------------- #


DECLARED_PUBLIC_SOURCES = frozenset({"config", "marker"})


def route_public(ctx, func: FunctionInfo, comments: Optional[Dict[int, str]] = None) -> Optional[str]:
    """``config`` | ``marker`` when the route is declared public; ``path`` when only a health-check naming
    convention suggests it (not a declaration: the exposure stays undetermined); else None."""
    route = func.route
    if route is None:
        return None
    path = route.path or ""
    globs = []
    if ctx.facts is not None:
        globs = [g for g in ctx.facts.config.get("public_routes", []) if isinstance(g, str)]
    if globs:
        prefix = _router_prefix(ctx, route.receiver.split(".")[0])
        candidates = {path, (prefix + path) if prefix else path, path.rstrip("/"), ((prefix + path) if prefix else path).rstrip("/")}
        for g in globs:
            if any(fnmatch.fnmatch(c, g) or fnmatch.fnmatch("/" + c.lstrip("/"), g) for c in candidates):
                return "config"
        # v2.0: effective paths composed through include_router; every mount must match (a router mounted both
        # behind a public prefix and a private one is not public), and nothing is matched for unresolved routers
        effective, resolution = effective_paths(ctx, func)
        if effective and resolution in ("resolved", "ambiguous"):
            if all(any(fnmatch.fnmatch(e, g) or fnmatch.fnmatch(e.rstrip("/") or "/", g) for g in globs) for e in effective):
                return "config"
    comments = comments if comments is not None else line_comments(ctx.source)
    lines = {func.lineno, *[d.lineno for d in getattr(func.node, "decorator_list", [])]}
    if any(PUBLIC_MARKER_RE.search(comments.get(line, "") or "") for line in lines):
        return "marker"
    # declarations first; the naming convention is only consulted when nothing was declared
    if PUBLIC_PATH_RE.match(path):
        # "" and "/" are public landings only on the application itself; on a prefixed router they are
        # the collection endpoint (`@router.get("")`), which v1.4 wrongly treated as public
        if path.strip("/") or route.receiver.split(".")[0] in {"app", "application", "api"}:
            return "path"
    return None


def effective_paths(ctx, func: FunctionInfo) -> Tuple[List[str], str]:
    """``(paths, resolution)`` for a route: the paths a client actually calls, composed from the application(s)
    through ``include_router`` prefixes (facts.router_effective_prefixes). Routes registered on an application
    object resolve to their own path; routers that are never mounted are ``unmounted``; a non-constant prefix on
    the chain yields ``unresolved`` and no paths (nothing is invented)."""
    route = func.route
    if route is None or ctx.facts is None:
        return [], "unmounted"
    root = route.receiver.split(".")[0]
    path = route.path or ""
    module = ctx.facts.file_module.get(ctx.relpath, "")
    names = {n for n in (func.resolve([root]), f"{module}.{root}" if module else root) if n}
    if names & ctx.facts.app_bindings:
        return [path], "resolved"
    for name in names:
        if name in ctx.facts.router_prefix_resolution:
            resolution = ctx.facts.router_prefix_resolution[name]
            if resolution == "unresolved":
                return [], "unresolved"
            return sorted({_join(prefix, path) for prefix in ctx.facts.router_effective_prefixes.get(name, [])}), resolution
    return [], "unmounted"


def _join(prefix: str, path: str) -> str:
    joined = (prefix.rstrip("/") + "/" + path.lstrip("/")) if path.strip("/") else (prefix or path)
    if not joined.startswith("/"):
        joined = "/" + joined
    return joined or "/"


def _router_prefix(ctx, receiver_root: str) -> str:
    for stmt in ctx.tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and any(isinstance(t, ast.Name) and t.id == receiver_root for t in stmt.targets):
            for kw in stmt.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
    return ""


def router_dependency_text(ctx, func: FunctionInfo) -> str:
    """Source text of ``dependencies=[...]`` on the route decorator and on its router (same file)."""
    route = func.route
    if route is None:
        return ""
    texts: List[str] = []
    if route.decorator is not None:
        for kw in route.decorator.keywords:
            if kw.arg == "dependencies":
                texts.append(ast.unparse(kw.value) if hasattr(ast, "unparse") else "")
    receiver_root = route.receiver.split(".")[0]
    for stmt in ctx.tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and any(isinstance(t, ast.Name) and t.id == receiver_root for t in stmt.targets):
            for kw in stmt.value.keywords:
                if kw.arg == "dependencies":
                    texts.append(ast.unparse(kw.value) if hasattr(ast, "unparse") else "")
    return " ".join(texts)


def _depends_target(call: ast.Call, resolve) -> Optional[Tuple[Optional[str], Optional[List[str]], str]]:
    """``(dotted, raw parts, label)`` of a ``Depends(x)`` / ``Security(x, scopes=...)`` call, or None."""
    parts = dotted_name(call.func)
    if not parts or parts[-1] not in {"Depends", "Security"} or not call.args:
        return None
    arg0 = call.args[0]
    inner = dotted_name(arg0)
    label = inner[-1] if inner else ""
    if inner is None and isinstance(arg0, ast.Call):  # Depends(require("x")): a factory and its arguments
        inner = dotted_name(arg0.func)
        label = ast.unparse(arg0) if hasattr(ast, "unparse") else (inner[-1] if inner else "")
    if call.keywords and hasattr(ast, "unparse"):
        label = f"{parts[-1]}({label}, " + ", ".join(ast.unparse(k) for k in call.keywords) + ")"
    return (resolve(inner) if inner else None, inner, label)


def router_dependency_targets(ctx, func: FunctionInfo) -> List[Tuple[Optional[str], Optional[List[str]], str]]:
    """The ``Depends()``/``Security()`` targets of ``dependencies=[...]`` on the route decorator and on its router (same file)."""
    route = func.route
    if route is None:
        return []
    values: List[ast.AST] = []
    if route.decorator is not None:
        values += [kw.value for kw in route.decorator.keywords if kw.arg == "dependencies"]
    receiver_root = route.receiver.split(".")[0]
    for stmt in ctx.tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and any(isinstance(t, ast.Name) and t.id == receiver_root for t in stmt.targets):
            values += [kw.value for kw in stmt.value.keywords if kw.arg == "dependencies"]
    out = []
    for value in values:
        for sub in ast.walk(value):
            if isinstance(sub, ast.Call):
                target = _depends_target(sub, func.resolve)
                if target is not None:
                    out.append(target)
    return out


def policy_records(ctx, dotted: Optional[str], raw: Optional[List[str]], dependency_record, dependency_chain) -> List[object]:
    """The function records that can carry policy evidence for a dependency target: its dependency chain, the
    ``__call__`` chain of a class-based dependency, and the closures defined inside each of them (``require("x")``
    returns one). Empty when the target is not in the project (a library dependency): nothing can be verified."""
    graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
    records = list(dependency_chain(ctx, dependency_record(ctx, dotted, raw)))
    if not records and graph is not None:
        module = ctx.facts.file_module.get(ctx.relpath, "") if ctx.facts is not None else ""
        candidates = [dotted] + ([f"{module}.{'.'.join(raw)}" if module else ".".join(raw)] if raw else [])
        for cand in candidates:
            cls = graph.canonical_class(cand) if cand else None
            if cls:
                records = list(dependency_chain(ctx, graph.functions.get(f"{cls}.__call__")))
                break
    if graph is not None and records:
        nested = getattr(graph, "_nested_function_index", None)
        if nested is None:
            nested = {}
            for key in graph.functions:
                head = key.rsplit(".", 1)[0] if "." in key else key
                nested.setdefault(head, []).append(key)
            graph._nested_function_index = nested
        seen = {rec.key for rec in records}
        for rec in list(records):
            for key in nested.get(rec.key, []):
                if key not in seen:
                    seen.add(key)
                    records.append(graph.functions[key])
    return records


def policy_evidence_in(records, graph=None) -> List[str]:
    """``"<function>: line N: condition on role"`` for every record of the chain that shows a privilege decision, plus the
    decisions of the project functions each record calls directly (``if not user_is_allowed(user): raise``: the
    predicate lives one call away; followed one level, never by name)."""
    out: List[str] = []
    for rec in records:
        out.extend(f"{rec.name}: {e}" for e in getattr(rec, "policy_evidence", []) or [])
        if graph is None:
            continue
        for site in getattr(rec, "calls", []) or []:
            key = graph.canonical_function(site.target) if site.target else None
            callee = graph.functions.get(key) if key else None
            if callee is not None and callee is not rec:
                out.extend(f"{rec.name} -> {callee.name}: {e}" for e in getattr(callee, "policy_evidence", []) or [])
    return out


def _policy_call_verified(ctx, graph, node: ast.Call, callee_parts: List[str], dependency_record, dependency_chain) -> Optional[str]:
    """The first evidence line of the project function a policy-like call resolves to, or None."""
    if graph is None:
        return None
    status, key = graph.site_target(ctx.relpath, node.lineno, node.col_offset)
    if status != "project" or not key:
        return None
    rec = graph.functions.get(key)
    if rec is None:
        return None
    records = policy_records(ctx, key, None, dependency_record, dependency_chain) or [rec]
    evidence = policy_evidence_in(records, graph)
    return evidence[0] if evidence else None


def path_params(path: Optional[str]) -> List[str]:
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", path or "")


def names_in(node: ast.AST) -> Set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def decorator_authz(func: FunctionInfo) -> Optional[str]:
    """``@requires("admin")``-style decorators: the decorator name and its arguments name a policy (a name only; see
    ``decorator_policy`` for the evidence-bearing version used by the contracts and the rules)."""
    for dec in getattr(func.node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = dotted_name(target)
        if not parts:
            continue
        if parts[-1] in {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}:
            continue  # the route decorator itself
        text = ast.unparse(dec) if hasattr(ast, "unparse") else ".".join(parts)
        if DECORATOR_AUTHZ_RE.search(parts[-1]) or (isinstance(dec, ast.Call) and ROLE_RE.search(text)):
            return text
    return None


# decorators whose authorization semantics are documented by the framework that ships them: recognised by their
# resolved import origin, never by a bare name (``from starlette.authentication import requires`` raises 403 or
# redirects when the request's auth scopes lack the required ones)
KNOWN_AUTHZ_DECORATORS = frozenset({"starlette.authentication.requires"})


def decorator_policy(ctx, func: FunctionInfo, dependency_record, dependency_chain) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(decorator text, evidence, named_only reason)`` for the first policy-like decorator of a route (v2.0 acceptance):
    the kind ``decorator`` is recorded only with evidence, which is either the decorator's resolved origin being a known
    framework authorization decorator, or a privilege decision visible in the project function it resolves to (its
    closures included). A policy-like name that resolves to code without a decision, or to no project code at all,
    is a named-only signal."""
    text = decorator_authz(func)
    if text is None:
        return None, None, None
    graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
    for dec, dotted in zip(getattr(func.node, "decorator_list", []), func.decorators):
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = dotted_name(target) or []
        if not parts or parts[-1] in {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}:
            continue
        this_text = ast.unparse(dec) if hasattr(ast, "unparse") else ".".join(parts)
        if this_text != text:
            continue
        canonical = (ctx.facts.canonical(dotted) if (dotted and ctx.facts is not None) else dotted) or dotted
        if canonical in KNOWN_AUTHZ_DECORATORS or dotted in KNOWN_AUTHZ_DECORATORS:
            return text, f"{canonical or dotted}: framework decorator with documented scope enforcement", None
        records = policy_records(ctx, dotted, parts, dependency_record, dependency_chain) if dotted else []
        evidence = policy_evidence_in(records, graph)
        if evidence:
            return text, evidence[0], None
        why = f"no decision is visible in {' -> '.join(r.name for r in records)}" if records else "its implementation is not in the project"
        return text, None, f"decorator {text}: named like a policy, {why}"
    return text, None, f"decorator {text}: named like a policy, its implementation is not in the project"


# --------------------------------------------------------------------------- #
# contract construction
# --------------------------------------------------------------------------- #


def principal_kind_of(func: FunctionInfo, providers, principal: Set[str], used: Set[str], dependency_record, ctx) -> str:
    """What the route's authentication hands it (v1.8 review):

    * ``value``: at least one authentication parameter is used in the body, or its dependency returns something;
    * ``none``: every authentication parameter is unused (``_: Authenticated``) and every resolvable dependency
      behind it is annotated ``-> None`` and never assigns ``request.state``: a token check that identifies no
      caller (shared credential). Such a route has no principal a resource could be bound to;
    * ``unknown``: no authentication parameter at all (router/application dependencies, middleware).
    """
    if not principal:
        return "unknown"
    verdict = "none"
    for param, dotted, raw, _label in providers:
        if param not in principal:
            continue
        if param in used and not param.startswith("_"):
            return "value"
        record = dependency_record(ctx, dotted, raw)
        if record is None or not getattr(record, "returns_none", False) or getattr(record, "writes_request_state", False):
            verdict = "value"
    if any(p in used for p in principal if not p.startswith("_")):
        return "value"
    return verdict


def build_contracts(ctx, providers_of, dependency_chain, dependency_record, param_signal, router_signal) -> List[RouteContract]:
    """One contract per route in the file. The helpers are injected from rules.security so the
    two modules share one notion of dependency chains and signals."""
    comments = line_comments(ctx.source)
    graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
    out: List[RouteContract] = []
    for func in ctx.functions:
        route = func.route
        if route is None:
            continue
        c = RouteContract(file=ctx.relpath, line=func.lineno, function=func.qualname, method=route.method, path=route.path)
        source = route_public(ctx, func, comments)
        if source:
            c.public_source = source
            c.public = source in DECLARED_PUBLIC_SOURCES  # a naming convention never declares anything
        used = {n.id for n in ast.walk(func.node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        params = [a.arg for a in func.params()]
        tenant_names = tenant_param_set(ctx)
        c.tenant_params = sorted(p for p in params if p in tenant_names and p in used)
        path_ids = set(path_params(route.path))
        c.resource_params = sorted(p for p in params if p in path_ids and p not in tenant_names and RESOURCE_PARAM_RE.search(p) and p in used)

        # authentication
        providers = providers_of(ctx, func)
        principal: Set[str] = set()
        for param, dotted, raw, label in providers:
            chain = dependency_chain(ctx, dependency_record(ctx, dotted, raw))
            chain_names = [rec.name for rec in chain]
            auth = AUTH_RE.search(label) or any(AUTH_RE.search(n) for n in chain_names) or AUTH_RE.search(param) or any(AUTH_RE.search(p) for rec in chain for p in rec.params if p not in tenant_names)
            if auth:
                principal.add(param)
                c.authentication_evidence.append(f"{param}: {label}" + (f" -> {' -> '.join(chain_names)}" if chain_names else ""))
            # authorization kinds carried by the dependency. v2.0 hardening: a role/permission kind needs structural
            # evidence in the dependency's own code (a condition over a role/scope/permission attribute or predicate,
            # anywhere in its chain, its __call__ or its closures); a name such as require_admin is recorded as
            # named_only and leaves the route's authorization unknown
            records = policy_records(ctx, dotted, raw, dependency_record, dependency_chain)
            evidence = policy_evidence_in(records, graph)
            named_like_policy = bool(ROLE_RE.search(label) and not MEMBERSHIP_RE.search(label)) or any(ROLE_RE.search(n) and not MEMBERSHIP_RE.search(n) for n in chain_names)
            if "scopes=" in label or "Security(" in label:
                c.authorization.append("scopes")
                c.authorization_evidence.append(label)
            elif evidence:
                c.authorization.append("role")
                c.authorization_evidence.append(f"{param}: {label}: {evidence[0]}")
            elif named_like_policy:
                why = f"no check on the principal is visible in {' -> '.join(chain_names)}" if records else "its implementation is not in the project"
                c.named_only.append(f"{param}: {label}: named like a policy, {why}")
            if any(rec.tenant_params or any(p in tenant_names for p in rec.params) for rec in chain):
                c.authorization.append("membership")
                c.authorization_evidence.append(f"{param}: {label}")
            elif MEMBERSHIP_RE.search(label):
                c.named_only.append(f"{param}: {label}: named like a membership check, but no dependency in its chain receives the tenant")
            if any(set(rec.params) & path_ids - tenant_names for rec in chain) and chain:
                c.authorization.append("resource_dependency")
                c.authorization_evidence.append(f"{param}: {label} receives {sorted(set().union(*[set(rec.params) & path_ids for rec in chain]))}")
        for arg in func.params():
            if AUTH_RE.search(arg.arg) and arg.arg not in tenant_names and arg.arg not in principal and arg.arg not in {"self", "cls"}:
                principal.add(arg.arg)
        c.principal_params = sorted(principal)
        c.principal_kind = principal_kind_of(func, providers, principal, used, dependency_record, ctx)
        dep_text = router_dependency_text(ctx, func)
        optional = False
        for param, dotted, raw, label in providers:
            if param not in principal:
                continue
            chain_names = [rec.name for rec in dependency_chain(ctx, dependency_record(ctx, dotted, raw))]
            annotation = next((a.annotation for a in func.params() if a.arg == param), None)
            ann_text = ast.unparse(annotation) if (annotation is not None and hasattr(ast, "unparse")) else ""
            if OPTIONAL_RE.search(label) or any(OPTIONAL_RE.search(n) for n in chain_names) or "| None" in ann_text or "Optional[" in ann_text or ann_text.startswith("Union[") and "None" in ann_text:
                optional = True  # `User | None` / `Optional[User]`: anonymous callers accepted; a bare `_: None = Depends(check)` is not optional
        if principal or param_signal(ctx, func):
            c.authentication = "optional" if (optional and not router_signal(ctx, func)) else "present"
            if c.authentication == "optional":
                c.authentication_evidence.append("anonymous callers are accepted by the dependency")
        elif router_signal(ctx, func):
            c.authentication = "present"
            c.authentication_evidence.append("router/application dependencies or middleware" + (f": {dep_text}" if dep_text else ""))
        if dep_text:
            # v2.0 hardening: router-level dependencies contribute a kind only when their code shows one (scopes, a
            # privilege decision, a tenant received); an authentication-only or a named-only router dependency keeps
            # the route authenticated but leaves its authorization unknown
            kinds: List[str] = []
            for dotted, raw, label in router_dependency_targets(ctx, func):
                records = policy_records(ctx, dotted, raw, dependency_record, dependency_chain)
                chain = dependency_chain(ctx, dependency_record(ctx, dotted, raw))
                evidence = policy_evidence_in(records, graph)
                if "scopes=" in label or "Security(" in label:
                    kinds.append("scopes")
                elif evidence:
                    kinds.append("role")
                    c.authorization_evidence.append(f"router: {label}: {evidence[0]}")
                elif ROLE_RE.search(label) and not MEMBERSHIP_RE.search(label):
                    why = f"no check on the principal is visible in {' -> '.join(rec.name for rec in chain)}" if records else "its implementation is not in the project"
                    c.named_only.append(f"router: {label}: named like a policy, {why}")
                if any(rec.tenant_params or any(p in tenant_names for p in rec.params) for rec in chain):
                    kinds.append("membership")
                elif MEMBERSHIP_RE.search(label):
                    c.named_only.append(f"router: {label}: named like a membership check, but no dependency in its chain receives the tenant")
            if kinds:
                c.authorization.extend(kinds)
                c.authorization.append("router_dependencies")
            c.authorization_evidence.append(dep_text)
        dec, dec_evidence, dec_named = decorator_policy(ctx, func, dependency_record, dependency_chain)
        if dec and dec_evidence:
            c.authorization.append("decorator")
            c.authorization_evidence.append(f"{dec}: {dec_evidence}")
        elif dec_named:
            c.named_only.append(dec_named)

        # body: principal flowing with the resource, authorization-service calls, tenant comparisons
        resource_set = set(c.resource_params)
        tenant_set = set(c.tenant_params)
        for node in func.scope_nodes():
            if isinstance(node, (ast.Compare, ast.BoolOp, ast.Assert, ast.If)):
                names = names_in(node.test if isinstance(node, ast.If) else node)
                if names & principal and names & (tenant_set | resource_set):
                    c.authorization.append("tenant_comparison" if names & tenant_set else "principal_with_resource")
                    c.authorization_evidence.append(f"line {node.lineno}: compared with the principal")
            if not isinstance(node, ast.Call):
                continue
            callee_parts = dotted_name(node.func) or []
            callee_name = callee_parts[-1] if callee_parts else ""
            arg_names: Set[str] = set()
            for a in [*node.args, *[k.value for k in node.keywords]]:
                arg_names |= names_in(a)
            with_principal = bool(arg_names & principal)
            with_resource = bool(arg_names & resource_set)
            with_tenant = bool(arg_names & tenant_set)
            if callee_name and AUTHZ_CALL_RE.search(callee_name) and with_principal and callee_name not in {"require", "requires"} and _policy_call_verified(ctx, graph, node, callee_parts, dependency_record, dependency_chain):
                # v2.0 acceptance: a call named like a policy counts only when the project function it resolves to shows a
                # privilege decision (itself or one call deep); an unresolvable or decision-less callee is a name
                c.authorization.append("authorization_call")
                c.authorization_evidence.append(f"line {node.lineno}: {'.'.join(callee_parts)}(): {_policy_call_verified(ctx, graph, node, callee_parts, dependency_record, dependency_chain)}")
            elif with_principal and (with_resource or with_tenant) and callee_parts:
                status, key = graph.site_target(ctx.relpath, node.lineno, node.col_offset) if graph is not None else ("dynamic", None)
                if status == "project" or callee_name not in {"print", "format", "str", "dict"}:
                    c.authorization.append("principal_with_resource" if with_resource else "tenant_comparison")
                    c.authorization_evidence.append(f"line {node.lineno}: {'.'.join(callee_parts)}() receives principal and {'resource' if with_resource else 'tenant'}")
            elif with_resource and not with_principal and graph is not None:
                status, key = graph.site_target(ctx.relpath, node.lineno, node.col_offset)
                if status == "project" and key:
                    callee = graph.functions.get(key)
                    # the callee itself may take a principal or call an authorization service
                    takes_principal = callee is not None and (callee.principal_params or any(AUTH_RE.search(p) for p in callee.params if p not in tenant_names))
                    calls_authz = callee is not None and any((s.attr or (s.target or "").rsplit(".", 1)[-1]) and AUTHZ_CALL_RE.search(s.attr or (s.target or "").rsplit(".", 1)[-1]) for s in callee.calls)
                    if not takes_principal and not calls_authz:
                        c.resource_calls_without_principal.append(f"{callee.key if callee else key}({node.lineno})")
        c.authorization = sorted(set(c.authorization))
        if c.authentication == "present" and not c.authorization and (c.resource_params or c.tenant_params):
            c.evidence = "unknown"
        elif c.authentication == "absent" and not c.public:
            c.evidence = "unknown"  # includes the health-convention routes: convention is not evidence
        c.full_path = (_router_prefix(ctx, route.receiver.split(".")[0]) or "") + (route.path or "")
        c.effective_paths, c.path_resolution = effective_paths(ctx, func)
        out.append(c)
    return out
