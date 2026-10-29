"""Per-function summaries for the interprocedural pass (v1.5).

A summary is a small record built from one function's AST: its parameters and
their declared kinds, the calls it makes (with how names flow into arguments),
whether it blocks, writes, creates sessions or sets tenant context, and what a
dependency returns. Summaries contain no AST nodes, so a whole project fits in
memory; the call graph in ``callgraph.py`` links them.

Nothing here executes inspected code. Every resolution is syntactic.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union, TYPE_CHECKING

from .kinds import (
    ASYNC_SESSION_COROUTINES,
    CLIENT_KINDS,
    FACTORY_KINDS,
    KIND_SESSION,
    SESSION_KINDS,
    SYNC_SESSION_IO,
    TYPE_KINDS,
    function_kinds,
    kind_of_call,
    kind_of_type,
    receiver_kind,
    receiver_name,
)
from .resolver import FunctionInfo, Scope, annotation_to_dotted, awaited_call_ids, collect_functions, dotted_name, iter_scope_nodes, module_imports, resolve_in_scopes

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ProjectFacts

HTTP_WRITE_VERBS = frozenset({"post", "put", "patch", "delete", "request", "send"})
HTTP_MODULES = ("requests.", "httpx.", "aiohttp.")
DB_WRITE_METHODS = frozenset({"commit", "add", "add_all", "delete", "merge", "flush", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"})
RETRY_DECORATORS = ("tenacity.retry", "tenacity.", "backoff.on_exception", "backoff.on_predicate", "stamina.retry", "retrying.retry", "retry.retry")
ITERATOR_WRAPPERS = frozenset(
    {
        "typing.AsyncIterator", "typing.AsyncGenerator", "typing.Iterator", "typing.Generator", "typing.Iterable", "typing.AsyncIterable",
        "collections.abc.AsyncIterator", "collections.abc.AsyncGenerator", "collections.abc.Iterator", "collections.abc.Generator",
        "collections.abc.Iterable", "collections.abc.AsyncIterable", "contextlib.AbstractAsyncContextManager", "contextlib.AbstractContextManager",
    }
)
TENANT_CONTEXT_MARKERS = ("set local", "set_config(", "set role")
IDEMPOTENCY_MARKERS = ("idempotency", "idempotent", "on conflict", "on_conflict", "dedup", "request_id", "requestid", "x-request-id")
PRINCIPAL_PARAM_RE = ("user", "principal", "tenant", "actor", "identity", "member", "org", "account_id", "auth")
BLOCKING_DIRECT: Dict[str, str] = {  # same table as async_rules.BLOCKING_CALLS, duplicated to avoid an import cycle
    "time.sleep": "high", "urllib.request.urlopen": "high", "urllib.request.urlretrieve": "high",
    "subprocess.run": "high", "subprocess.call": "high", "subprocess.check_call": "high", "subprocess.check_output": "high",
    "subprocess.getoutput": "high", "subprocess.getstatusoutput": "high", "os.system": "high", "os.popen": "medium",
    "socket.create_connection": "medium", "smtplib.SMTP": "medium", "psycopg2.connect": "medium", "pymysql.connect": "medium", "sqlite3.connect": "medium",
}
for _v in ("get", "post", "put", "patch", "delete", "head", "options", "request"):
    BLOCKING_DIRECT[f"requests.{_v}"] = "high"
    BLOCKING_DIRECT[f"httpx.{_v}"] = "high"
BLOCKING_DIRECT["httpx.stream"] = "high"


@dataclass
class CallSite:
    lineno: int
    col: int
    target: Optional[str]  # dotted candidate ("app.svc.helper", "app.svc.Repo.get", "time.sleep")
    status: str  # candidate | dynamic
    receiver: Optional[str]  # receiver variable for attribute calls
    attr: Optional[str]
    arg_names: List[Tuple[Union[int, str], str]] = field(default_factory=list)  # (position|keyword, Name id)
    awaited: bool = False
    has_receiver: bool = False  # call through an instance (positional offset for `self`)


@dataclass
class FunctionRecord:
    key: str  # module.qualname
    file: str
    module: str
    qualname: str
    name: str
    is_async: bool
    lineno: int
    class_name: Optional[str]
    params: List[str]
    declared_kinds: Dict[str, str] = field(default_factory=dict)
    dependency_kinds: Dict[str, str] = field(default_factory=dict)  # from Depends(f) return analysis
    inferred_kinds: Dict[str, str] = field(default_factory=dict)  # from callers (propagation)
    depends: Dict[str, Optional[str]] = field(default_factory=dict)  # param -> dotted dependency callable
    returns_kind: Optional[str] = None
    returns_kind_source: Optional[str] = None  # annotation | yield | return
    calls: List[CallSite] = field(default_factory=list)
    blocking_direct: List[Tuple[int, str]] = field(default_factory=list)  # (line, symbol)
    writes: List[Tuple[int, str]] = field(default_factory=list)  # (line, "db"|"http")
    creates_session: bool = False
    sets_tenant_context: bool = False
    is_route: bool = False
    route_label: Optional[str] = None
    decorators: List[Optional[str]] = field(default_factory=list)
    retry_decorated: bool = False
    retry_loop: bool = False
    idempotency_signal: bool = False
    instance_of: Dict[str, str] = field(default_factory=dict)  # local var -> project class dotted
    principal_params: List[str] = field(default_factory=list)
    background_task_param: Optional[str] = None  # name of a BackgroundTasks parameter

    def kind_of(self, name: str) -> Tuple[Optional[str], Optional[str]]:
        """(kind, evidence) for a parameter/local name: declared > dependency > inferred."""
        if name in self.declared_kinds:
            return self.declared_kinds[name], "verified"
        if name in self.dependency_kinds:
            return self.dependency_kinds[name], "verified"
        if name in self.inferred_kinds:
            return self.inferred_kinds[name], "inferred"
        return None, None


@dataclass
class ClassRecord:
    key: str
    file: str
    methods: Dict[str, str] = field(default_factory=dict)  # method name -> function key
    bases: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #


def _depends_target(node: Optional[ast.AST], resolve) -> Optional[Tuple[bool, Optional[str]]]:
    """(is_depends, dotted_target) when ``node`` is/contains ``Depends(f)`` or ``Security(f)``."""
    if node is None:
        return None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            parts = dotted_name(sub.func)
            if parts and parts[-1] in {"Depends", "Security"}:
                if sub.args:
                    inner = dotted_name(sub.args[0])
                    return True, (resolve(inner) if inner else None), inner
                return True, None, None
    return None


def _unwrap_return_kind(annotation: Optional[ast.AST], resolve, facts) -> Optional[str]:
    """Kind returned/yielded according to an annotation such as AsyncIterator[AsyncSession]."""
    if annotation is None:
        return None
    direct = kind_of_type(annotation_to_dotted(annotation, resolve), facts)
    if direct:
        return direct
    if isinstance(annotation, ast.Subscript):
        base_parts = dotted_name(annotation.value)
        base = resolve(base_parts) if base_parts else None
        if base in ITERATOR_WRAPPERS:
            inner = annotation.slice
            if isinstance(inner, ast.Tuple) and inner.elts:
                inner = inner.elts[0]
            return kind_of_type(annotation_to_dotted(inner, resolve), facts)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _unwrap_return_kind(ast.parse(annotation.value, mode="eval").body, resolve, facts)
        except SyntaxError:
            return None
    return None


def _string_constants(node: ast.AST) -> List[str]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            out.append("".join(v.value for v in sub.values if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return out


def _has_retry_loop(func: FunctionInfo) -> bool:
    """A loop whose body catches exceptions and goes around again: the shape of a hand-written retry."""
    for node in func.scope_nodes():
        if isinstance(node, (ast.For, ast.While)):
            has_try = any(isinstance(n, ast.Try) for n in iter_scope_nodes(node.body))
            if not has_try:
                continue
            loop_text = ast.dump(node).lower()
            retry_vocabulary = any(tok in loop_text for tok in ("attempt", "retr", "tries"))
            if isinstance(node, ast.While):
                # a worker/poll loop (`while not stopping: try: ... except: log/backoff`) is not a
                # retry; require an explicit attempt counter (attempts += 1 / attempts < n) in the loop
                counter = any(
                    (isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and any(t in n.target.id.lower() for t in ("attempt", "retr", "tries")))
                    or (isinstance(n, ast.Compare) and any(isinstance(c, ast.Name) and any(t in c.id.lower() for t in ("attempt", "retr", "tries")) for c in [n.left, *n.comparators]))
                    for n in ast.walk(node)
                )
                if counter:
                    return True
                continue
            iter_text = ""
            if isinstance(node.iter, ast.Call):
                parts = dotted_name(node.iter.func)
                iter_text = ".".join(parts) if parts else ""
            if iter_text.endswith("range") or retry_vocabulary:
                return True
    return False


def build_summaries(
    rel: str,
    module: str,
    tree: ast.Module,
    facts: "ProjectFacts",
    local_factories: Dict[str, str],
) -> Tuple[List[FunctionRecord], List[ClassRecord]]:
    """Summaries for every function and class in one parsed module."""
    fastapi_imported = module_imports(tree, ("fastapi",))
    module_scope, functions = collect_functions(tree, fastapi_imported)
    awaited = awaited_call_ids(tree)
    module_defs: Dict[str, str] = {}  # local name -> module.qualname for top-level defs/classes
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_defs[stmt.name] = f"{module}.{stmt.name}" if module else stmt.name
    classes: Dict[str, ClassRecord] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            key = module_defs[stmt.name]
            bases = []
            for base in stmt.bases:
                parts = dotted_name(base)
                if parts:
                    bases.append(resolve_in_scopes([module_scope], parts) or module_defs.get(parts[0], ".".join(parts)))
            classes[key] = ClassRecord(key=key, file=rel, bases=bases)
    records: List[FunctionRecord] = []

    for func in functions:
        key = f"{module}.{func.qualname}" if module else func.qualname
        params = [a.arg for a in func.params()]
        kinds = function_kinds(func, facts, local_factories)  # declared + local assignments/with
        declared = {p: kinds[p] for p in params if p in kinds}
        rec = FunctionRecord(
            key=key, file=rel, module=module, qualname=func.qualname, name=func.name, is_async=func.is_async,
            lineno=func.lineno, class_name=func.class_name, params=params, declared_kinds=declared,
            decorators=list(func.decorators), is_route=func.route is not None,
        )
        if func.route:
            rec.route_label = f"{func.route.method} {func.route.path or ''}".strip()
        if func.class_name and func.parent is None:
            cls_key = module_defs.get(func.class_name)
            if cls_key and cls_key in classes:
                classes[cls_key].methods[func.name] = key
        rec.retry_decorated = any(d and d.startswith(RETRY_DECORATORS) for d in func.decorators)
        rec.retry_loop = _has_retry_loop(func)

        # parameters: Depends targets, principal-looking names, BackgroundTasks
        defaults = func.param_defaults()
        for arg in func.params():
            dep = _depends_target(defaults.get(arg.arg), func.resolve) or _depends_target(arg.annotation, func.resolve)
            if dep:
                _, dotted, parts = dep
                if dotted is None and parts and parts[0] in module_defs:
                    dotted = module_defs[parts[0]] + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                rec.depends[arg.arg] = dotted
            ann = annotation_to_dotted(arg.annotation, func.resolve) or ""
            if ann.endswith("BackgroundTasks") or arg.arg == "background_tasks":
                rec.background_task_param = arg.arg
            if any(tok in arg.arg.lower() for tok in PRINCIPAL_PARAM_RE) and arg.arg not in {"self", "cls"}:
                rec.principal_params.append(arg.arg)
            # instances of project classes received as parameters (local, earlier files, or aliases)
            if ann:
                canon = ann if (ann in classes or ann in facts.classes) else facts.class_aliases.get(ann)
                if canon is None and ann.rsplit(".", 1)[-1] in module_defs and module_defs[ann.rsplit(".", 1)[-1]] in classes:
                    canon = module_defs[ann.rsplit(".", 1)[-1]]
                if canon:
                    rec.instance_of[arg.arg] = canon

        # return / yield kind for dependency functions
        rk = _unwrap_return_kind(getattr(func.node, "returns", None), func.resolve, facts)
        if rk:
            rec.returns_kind, rec.returns_kind_source = rk, "annotation"
        else:
            for node in func.scope_nodes():
                value = None
                if isinstance(node, ast.Yield) and isinstance(node.value, ast.Name):
                    value, src = node.value.id, "yield"
                elif isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
                    value, src = node.value.id, "return"
                if value and kinds.get(value) in SESSION_KINDS | CLIENT_KINDS:
                    rec.returns_kind, rec.returns_kind_source = kinds[value], src
                    break

        # body scan: calls, blocking, writes, factories, tenant context, idempotency, instances
        texts = " ".join(_string_constants(func.node)).lower()
        rec.sets_tenant_context = any(m in texts for m in TENANT_CONTEXT_MARKERS)
        rec.idempotency_signal = any(m in texts for m in IDEMPOTENCY_MARKERS) or any(
            any(m in a.arg.lower() for m in ("idempot", "dedup")) for a in func.params()
        )
        for node in func.scope_nodes():
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
                cparts = dotted_name(node.value.func)
                if cparts:
                    cdotted = func.resolve(cparts) or module_defs.get(cparts[0])
                    if cdotted:
                        canon = cdotted if (cdotted in classes or cdotted in facts.classes) else facts.class_aliases.get(cdotted)
                        if canon:
                            rec.instance_of[node.targets[0].id] = canon
            if not isinstance(node, ast.Call):
                continue
            call = node
            parts = dotted_name(call.func)
            site = CallSite(lineno=call.lineno, col=call.col_offset, target=None, status="dynamic", receiver=receiver_name(call), attr=call.func.attr if isinstance(call.func, ast.Attribute) else None, awaited=id(call) in awaited)
            for i, arg in enumerate(call.args):
                if isinstance(arg, ast.Name):
                    site.arg_names.append((i, arg.id))
                elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                    site.arg_names.append((i, arg.value.id))
            for kw in call.keywords:
                if kw.arg and isinstance(kw.value, ast.Name):
                    site.arg_names.append((kw.arg, kw.value.id))
            dotted = func.resolve(parts) if parts else None
            if dotted:
                site.target, site.status = dotted, "candidate"
            elif parts:
                root = parts[0]
                # `self.method()` / `instance.method()` resolve; `self.attr.method()` chains stay dynamic
                if root in {"self", "cls"} and func.class_name and len(parts) == 2:
                    site.target = f"{module_defs.get(func.class_name, func.class_name)}.{parts[1]}"
                    site.status, site.has_receiver = "candidate", True
                elif root in module_defs and len(parts) <= 2:
                    site.target = module_defs[root] + ("." + parts[1] if len(parts) == 2 else "")
                    site.status = "candidate"
                elif root in rec.instance_of and len(parts) == 2:
                    site.target = f"{rec.instance_of[root]}.{parts[1]}"
                    site.status, site.has_receiver = "candidate", True
            rec.calls.append(site)
            # blocking and writes
            if dotted in BLOCKING_DIRECT:
                rec.blocking_direct.append((call.lineno, dotted))
            rk_recv = receiver_kind(call, kinds)
            if rk_recv == KIND_SESSION and site.attr in SYNC_SESSION_IO:
                rec.blocking_direct.append((call.lineno, f"Session.{site.attr}"))
            if dotted and dotted.startswith(HTTP_MODULES) and dotted.rsplit(".", 1)[-1] in HTTP_WRITE_VERBS:
                rec.writes.append((call.lineno, "http"))
            elif rk_recv in CLIENT_KINDS and site.attr in HTTP_WRITE_VERBS:
                rec.writes.append((call.lineno, "http"))
            elif rk_recv in SESSION_KINDS and site.attr in DB_WRITE_METHODS:
                rec.writes.append((call.lineno, "db"))
            elif site.attr in {"commit", "add", "add_all"} and site.receiver in {"session", "db", "db_session"}:
                rec.writes.append((call.lineno, "db"))
            if dotted in FACTORY_KINDS or kind_of_call(call, func.resolve, facts, local_factories) in SESSION_KINDS:
                rec.creates_session = True
        records.append(rec)
    return records, list(classes.values())
