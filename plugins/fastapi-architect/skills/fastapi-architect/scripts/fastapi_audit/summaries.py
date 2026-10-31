"""Per-function summaries for the interprocedural pass (v1.5, extended in v1.6).

A summary is a small record built from one function's AST: its parameters and
their declared kinds, the calls it makes (with how names flow into arguments),
whether it blocks, writes, creates sessions or sets tenant context, and what a
dependency returns. Summaries contain no AST nodes, so a whole project fits in
memory; the call graph in ``callgraph.py`` links them.

v1.6 adds, still syntactically: instance attributes typed in ``__init__``
(``self._session = session``), locals bound to the result of project calls,
functions handed as arguments (callbacks), builtin and external-typed receivers
classified at the call site, protocol/abstract markers on classes, and the
attribute receivers each method touches. Nothing here executes inspected code.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union, TYPE_CHECKING

from .kinds import (
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
from .resolver import (
    BUILTIN_NAMES,
    FunctionInfo,
    annotation_to_dotted,
    awaited_call_ids,
    collect_functions,
    dotted_name,
    iter_scope_nodes,
    module_imports,
    resolve_in_scopes,
)

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ProjectFacts

HTTP_WRITE_VERBS = frozenset({"post", "put", "patch", "delete", "request", "send"})
NETWORK_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request", "send", "stream"})
SLEEP_CALLS = frozenset({"time.sleep", "asyncio.sleep", "anyio.sleep", "trio.sleep"})
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
PROTOCOL_BASES = frozenset({"typing.Protocol", "typing_extensions.Protocol"})
ABSTRACT_BASES = frozenset({"abc.ABC"})
ABSTRACT_DECORATORS = frozenset({"abc.abstractmethod"})
CALLABLE_TYPES = frozenset({"typing.Callable", "collections.abc.Callable", "typing.Awaitable", "collections.abc.Awaitable", "typing.Coroutine", "collections.abc.Coroutine"})
ASGI_CALLBACK_TYPES = frozenset({
    "starlette.types.Send", "starlette.types.Receive", "starlette.types.ASGIApp", "starlette.types.Scope", "starlette.types.Message",
    "asgiref.typing.ASGISendCallable", "asgiref.typing.ASGIReceiveCallable", "asgiref.typing.ASGI3Application", "asgiref.typing.ASGIApplication",
})
ASGI_TRIPLE = ("scope", "receive", "send")
CALLABLE_NAME_HINTS = ("Callable", "Callback", "Handler", "Send", "Receive", "ASGIApp", "Awaitable")
TENANT_CONTEXT_MARKERS = ("set local", "set_config(", "set role")
IDEMPOTENCY_MARKERS = ("idempotency", "idempotent", "on conflict", "on_conflict", "dedup", "request_id", "requestid", "x-request-id")
PRINCIPAL_PARAM_RE = ("user", "principal", "tenant", "actor", "identity", "member", "org", "auth", "staff", "caller", "subject")
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
    status: str  # candidate | builtin | external | hint | dynamic
    receiver: Optional[str]  # receiver variable for attribute calls ("session", "self._session")
    attr: Optional[str]
    arg_names: List[Tuple[Union[int, str], str]] = field(default_factory=list)  # (position|keyword, Name id)
    awaited: bool = False
    has_receiver: bool = False  # call through an instance (positional offset for `self`)
    # v1.6
    hint: Optional[Tuple[str, ...]] = None  # ("attr", attr, method) | ("ret", callee_dotted, method) | ("callback", param)
    arg_callables: List[Tuple[Union[int, str], str]] = field(default_factory=list)  # (position|keyword, dotted function handed as argument)
    arg_roots: List[Tuple[Union[int, str], str]] = field(default_factory=list)  # (position|keyword, root Name of an attribute chain argument)
    origin: Optional[str] = None  # v1.8: why an unresolved receiver is unknown (loop_target, comprehension, untyped_param, ...)


@dataclass
class AttrInfo:
    """What a class stores in ``self.<attr>`` according to ``__init__``/class annotations."""

    type: Optional[str]  # dotted type of the value (annotation of the copied parameter, or the constructor called)
    kind: Optional[str]  # session/client kind when the type is one
    source: str  # init-param | init-call | init-annassign | classattr | init-other
    init_param: Optional[str] = None  # __init__ parameter copied into the attribute (kinds may arrive through callers)
    factory: Optional[str] = None  # kind produced when the attribute holds a session factory


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
    # v1.6
    returns_class: Optional[str] = None  # dotted project class named by the return annotation or `return Cls(...)`
    callable_params: Dict[str, Set[str]] = field(default_factory=dict)  # param -> function keys callers hand in (filled by the graph)
    self_attr_receivers: Set[str] = field(default_factory=set)  # attributes of self used as call receivers in this method
    local_origin: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # local -> ("ret", dotted callee) | ("external", "")
    tenant_params: List[str] = field(default_factory=list)  # parameters named like a tenant identifier
    network_calls: List[Tuple[int, str]] = field(default_factory=list)  # (line, symbol) HTTP calls and sleeps, any verb
    # v1.8
    returns_dotted: Optional[str] = None  # resolved return annotation (dotted), project or not
    returns_builtin: bool = False  # return annotation is a builtin type (dict, list[str], int, ...)
    inherited_kinds: Dict[str, str] = field(default_factory=dict)  # closure: kinds captured from the enclosing function (declared there)
    callback_kinds: Dict[str, str] = field(default_factory=dict)  # param -> "asgi" | "callable" | "untyped"
    asgi_signature: bool = False  # (scope, receive, send) handler shape
    owner_class: Optional[str] = None  # class whose `self` the function reads: the method's class, also for closures inside it
    returns_none: bool = False  # annotated `-> None`: a dependency that yields no value (a token check, a side effect)
    writes_request_state: bool = False  # assigns request.state.<x> / scope["state"]: an ambient principal may exist
    # v2.0 security hardening: lines where the function visibly decides on privileges (a condition or predicate over a
    # role/permission/scope-like attribute or call, or a Security(scopes=...) parameter). A name alone never counts:
    # ``def require_admin(): pass`` has no evidence, ``if user.role != "admin": raise`` has.
    policy_evidence: List[str] = field(default_factory=list)

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
    # v1.6
    attrs: Dict[str, AttrInfo] = field(default_factory=dict)
    is_protocol: bool = False
    is_abstract: bool = False
    abstract_methods: Set[str] = field(default_factory=set)
    lineno: int = 0


# --------------------------------------------------------------------------- #


def _depends_target(node: Optional[ast.AST], resolve) -> Optional[Tuple[bool, Optional[str], Optional[List[str]]]]:
    """(is_depends, dotted_target, raw parts) when ``node`` is/contains ``Depends(f)`` or ``Security(f)``."""
    if node is None:
        return None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            parts = dotted_name(sub.func)
            if parts and parts[-1] in {"Depends", "Security"}:
                if sub.args:
                    arg0 = sub.args[0]
                    inner = dotted_name(arg0)
                    if inner is None and isinstance(arg0, ast.Call):
                        inner = dotted_name(arg0.func)  # Depends(require(p.X)): the factory is the provider
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


def _direct_tries(loop: ast.AST) -> List[ast.Try]:
    """Try statements that are the loop body itself (possibly under an `if` directly in the body)."""
    out: List[ast.Try] = []
    for stmt in getattr(loop, "body", []):
        if isinstance(stmt, ast.Try):
            out.append(stmt)
        elif isinstance(stmt, ast.If):
            out.extend(s for s in [*stmt.body, *stmt.orelse] if isinstance(s, ast.Try))
    return out


def _retry_shaped(loop: ast.AST) -> bool:
    """A retry loop leaves on success (break/return inside the try) or goes around again from the
    handler (continue). Nested tries inside inner loops (per-item error handling) do not count."""
    for node in _direct_tries(loop):
        exits = any(isinstance(n, (ast.Break, ast.Return)) for n in iter_scope_nodes([*node.body, *node.orelse]))
        again = any(
            any(isinstance(n, ast.Continue) for n in iter_scope_nodes(h.body))
            or not any(isinstance(n, ast.Raise) for n in iter_scope_nodes(h.body))  # falls through to the next iteration
            for h in node.handlers
        )
        if exits or again:
            return True
    return False


def retry_try_ranges(func: FunctionInfo) -> List[Tuple[int, int]]:
    """Line ranges of the retried blocks (try bodies directly inside retry loops): a write counts
    as retried only when it happens there, not on a give-up or token-refresh path in a handler."""
    ranges: List[Tuple[int, int]] = []
    for node in func.scope_nodes():
        if isinstance(node, (ast.For, ast.While)) and _retry_shaped(node):
            for t in _direct_tries(node):
                if t.body:
                    ranges.append((t.body[0].lineno, getattr(t.body[-1], "end_lineno", t.body[-1].lineno)))
    return ranges


def _has_retry_loop(func: FunctionInfo) -> bool:
    """A loop whose body catches exceptions and goes around again: the shape of a hand-written retry."""
    for node in func.scope_nodes():
        if isinstance(node, (ast.For, ast.While)):
            if not _direct_tries(node):
                continue  # nested per-item error handling is not a retry of the loop
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
            if (iter_text.endswith("range") or retry_vocabulary) and _retry_shaped(node):
                return True
    return False


def _canonical_class(dotted: Optional[str], classes: Dict[str, ClassRecord], facts: "ProjectFacts", project_roots: Optional[Set[str]] = None) -> Optional[str]:
    """Project class key for a dotted name seen from this module, through re-exports and source-root
    aliases. Files are summarised in path order, so a class defined in a later file is not indexed yet:
    when ``project_roots`` is given, a dotted name under a project root is returned as-is and the call
    graph canonicalises it once every file has been read."""
    if not dotted:
        return None
    for candidate in (dotted, facts.canonical(dotted)):
        if not candidate:
            continue
        if candidate in classes or candidate in facts.classes:
            return candidate
        alias = facts.class_aliases.get(candidate)
        if alias:
            return alias
    if project_roots is not None and _is_project_dotted(dotted, project_roots, facts):
        canon = facts.canonical(dotted) or dotted
        if canon.rsplit(".", 1)[-1][:1].isupper():  # a capitalised project name: a class until proven otherwise
            return canon
    return None


def _is_project_dotted(dotted: Optional[str], project_roots: Set[str], facts: "ProjectFacts") -> bool:
    if not dotted:
        return False
    canon = facts.canonical(dotted) or dotted
    return canon.split(".")[0] in project_roots or dotted.split(".")[0] in project_roots


LITERAL_VALUES = (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.JoinedStr, ast.BinOp, ast.Compare, ast.BoolOp, ast.UnaryOp)


def _builtin_annotation(annotation: Optional[ast.AST], resolve) -> Optional[str]:
    """``builtins.<name>`` when an annotation is a builtin type (``str``, ``dict[str, int]``, ``Exception``), unshadowed."""
    if annotation is None:
        return None
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    if isinstance(base, ast.Constant) and isinstance(base.value, str):
        try:
            return _builtin_annotation(ast.parse(base.value, mode="eval").body, resolve)
        except SyntaxError:
            return None
    if isinstance(base, ast.Name) and base.id in BUILTIN_NAMES and resolve([base.id]) is None:
        return f"builtins.{base.id}"
    return None


def _class_attrs(cls: ast.ClassDef, functions: List[FunctionInfo], facts: "ProjectFacts", classes: Dict[str, ClassRecord], local_factories: Dict[str, str], module_defs: Optional[Dict[str, str]] = None) -> Dict[str, AttrInfo]:
    """Instance attributes with a visible type: ``self.x = <typed param>``, ``self.x = Cls()``,
    ``self.x: T = ...`` in ``__init__`` (first assignment wins), and class-level ``x: T`` annotations.
    v1.8: an annotation naming a class of the same module (``uow: UnitOfWork``) resolves through ``module_defs``."""
    attrs: Dict[str, AttrInfo] = {}
    module_defs = module_defs or {}

    def _typed(ann: Optional[ast.AST], resolve) -> Optional[str]:
        if ann is None:
            return None
        dotted = annotation_to_dotted(ann, resolve) or _builtin_annotation(ann, resolve)
        if dotted:
            return dotted
        base = ann.value if isinstance(ann, ast.Subscript) else ann
        if isinstance(base, ast.Constant) and isinstance(base.value, str):
            try:
                base = ast.parse(base.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(base, ast.Name) and base.id in module_defs and resolve([base.id]) is None:
            return module_defs[base.id]
        return None

    init = next((f for f in functions if f.class_name == cls.name and f.name == "__init__" and f.parent is None), None)
    if init is not None:
        params = {a.arg: a.annotation for a in init.params()}
        kinds = function_kinds(init, facts, local_factories)
        for node in init.scope_nodes():
            target = None
            value = None
            annotation = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value, annotation = node.target, node.value, node.annotation
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                continue
            name = target.attr
            if name in attrs:
                continue
            if annotation is not None:
                dotted = _typed(annotation, init.resolve)
                attrs[name] = AttrInfo(type=dotted, kind=kind_of_type(dotted, facts), source="init-annassign")
                continue
            if isinstance(value, ast.Name) and value.id in params:
                ann = params[value.id]
                dotted = _typed(ann, init.resolve)
                kind = kinds.get(value.id) or kind_of_type(dotted, facts)
                factory = None
                if ann is not None:
                    base = ann.value if isinstance(ann, ast.Subscript) else ann
                    bparts = dotted_name(base)
                    bdotted = init.resolve(bparts) if bparts else None
                    factory = FACTORY_KINDS.get(facts.canonical(bdotted) or bdotted or "")
                attrs[name] = AttrInfo(type=dotted, kind=kind, source="init-param", init_param=value.id, factory=factory)
            elif isinstance(value, ast.Call):
                cparts = dotted_name(value.func)
                cdotted = init.resolve(cparts) if cparts else None
                if cdotted is None and cparts and len(cparts) == 1 and cparts[0] in BUILTIN_NAMES:
                    cdotted = f"builtins.{cparts[0]}"
                kind = kind_of_call(value, init.resolve, facts, local_factories)
                attrs[name] = AttrInfo(type=cdotted, kind=kind, source="init-call")
            elif isinstance(value, ast.BoolOp) and value.values and isinstance(value.values[0], ast.Name) and value.values[0].id in params:
                ann = params[value.values[0].id]  # self.repo = repo or Default()
                dotted = _typed(ann, init.resolve)
                attrs[name] = AttrInfo(type=dotted, kind=kinds.get(value.values[0].id) or kind_of_type(dotted, facts), source="init-param", init_param=value.values[0].id)
            elif isinstance(value, LITERAL_VALUES) and not (isinstance(value, ast.Constant) and value.value is None):
                attrs[name] = AttrInfo(type="builtins.object", kind=None, source="init-literal")
            else:
                attrs[name] = AttrInfo(type=None, kind=None, source="init-other")
    # class-level annotations (dataclasses, pydantic, attrs): `repo: AccountPort`
    module_resolve = None
    for item in cls.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id not in attrs:
            if module_resolve is None:
                module_resolve = next((f.scopes[0] for f in functions), None)
            resolve = (lambda parts, s=module_resolve: resolve_in_scopes([s], parts)) if module_resolve is not None else (lambda parts: None)
            dotted = _typed(item.annotation, resolve)
            attrs[item.target.id] = AttrInfo(type=dotted, kind=kind_of_type(dotted, facts), source="classattr")
    return attrs


def _self_attr_is_external(cls_record: Optional[ClassRecord], attr: str, kinds: Dict[str, str]) -> bool:
    """``self.<attr>`` holds a library/builtin value (a session, a dict literal, a client) according to the class."""
    if f"self.{attr}" in kinds:
        return True
    info = cls_record.attrs.get(attr) if cls_record is not None else None
    return info is not None and bool(info.kind or (info.type or "").startswith("builtins."))


def _call_origin_class(inner: ast.AST, func: FunctionInfo, module_defs, facts, project_roots, kinds, param_external, param_builtin, rec: FunctionRecord, cls_record: Optional[ClassRecord] = None) -> Optional[str]:
    """``external`` / ``builtin`` when the value of a call is known to come from outside the project
    (library function, builtin, method on an external/builtin receiver, framework callback); None when unknown."""
    if not isinstance(inner, ast.Call):
        return None
    cparts = dotted_name(inner.func)
    if not cparts:
        if isinstance(inner.func, ast.Attribute) and isinstance(inner.func.value, (ast.Await, ast.Call)):
            return "external"  # (await x()).y(): a result of a result
        return None
    cdotted = func.resolve(cparts)
    if cdotted:
        if _is_project_dotted(cdotted, project_roots, facts):
            return None
        return "external"
    root = cparts[0]
    if len(cparts) == 1:
        if root in BUILTIN_NAMES and not any(root in sc.bound or root in sc.imports for sc in func.scopes):
            return "builtin"
        if root in rec.callback_kinds and rec.callback_kinds[root] == "asgi":
            return "external"  # message = await receive()
        return None
    origin = rec.local_origin.get(root, ("", ""))[0]
    if root in param_builtin or origin == "builtin":
        return "builtin"  # line.split(): methods of builtin values give builtin values
    if root in param_external or kinds.get(root) or origin == "external":
        return "external"
    if root == "self" and _self_attr_is_external(cls_record, cparts[1], kinds):
        return "external"  # item = self._queue.popleft()
    return None


def _bind_origin(rec: FunctionRecord, name: str, value: ast.AST, func: FunctionInfo, module_defs, classes, facts, project_roots, kinds, param_external, param_builtin, cls_record: Optional[ClassRecord] = None) -> None:
    """Record where a local comes from: a project instance, a project call (resolved later), an external or
    builtin value, or a framework callback result. Nothing here creates a project edge."""
    inner = value.value if isinstance(value, ast.Await) else value
    if isinstance(inner, LITERAL_VALUES) and name not in kinds and not (isinstance(inner, ast.Constant) and inner.value is None):
        rec.local_origin[name] = ("builtin", "")
        return
    if isinstance(inner, (ast.Attribute, ast.Subscript)) and name not in kinds:
        aparts = dotted_name(inner.value if isinstance(inner, ast.Subscript) else inner)
        root = aparts[0] if aparts else None
        if root and (root in param_external or root in param_builtin or rec.local_origin.get(root, ("", ""))[0] in {"external", "builtin"} or kinds.get(root)):
            rec.local_origin[name] = ("external", "")
        elif root == "self" and len(aparts) >= 2 and _self_attr_is_external(cls_record, aparts[1], kinds):
            rec.local_origin[name] = ("external", "")  # current = self.runs[run_id] where self.runs is a dict literal
        return
    if not isinstance(inner, ast.Call):
        return
    cparts = dotted_name(inner.func)
    cdotted = (func.resolve(cparts) or module_defs.get(cparts[0])) if cparts else None
    canon = _canonical_class(cdotted, classes, facts, project_roots)
    if canon:
        rec.instance_of[name] = canon
        return
    if cdotted and _is_project_dotted(cdotted, project_roots, facts):
        rec.local_origin[name] = ("ret", cdotted)
        return
    if cparts and len(cparts) == 1 and cparts[0] in module_defs:
        rec.local_origin[name] = ("ret", module_defs[cparts[0]])
        return
    if cparts and len(cparts) == 1 and cparts[0] in rec.callback_kinds:
        rec.local_origin[name] = ("external" if rec.callback_kinds[cparts[0]] == "asgi" else "callback_result", "")
        return
    origin = _call_origin_class(inner, func, module_defs, facts, project_roots, kinds, param_external, param_builtin, rec, cls_record)
    if origin and name not in kinds:
        rec.local_origin[name] = (origin, "")


def _lambda_target(node: ast.Lambda, func: FunctionInfo, module_defs, facts, project_roots) -> Optional[str]:
    """``lambda v: target(v)`` or ``lambda: target()``: the dotted project function the lambda forwards to."""
    body = node.body
    if isinstance(body, ast.Await):
        body = body.value
    if not isinstance(body, ast.Call):
        return None
    parts = dotted_name(body.func)
    if not parts:
        return None
    lambda_params = {a.arg for a in node.args.args}
    if any(not (isinstance(a, ast.Name) and a.id in lambda_params) for a in body.args):
        if body.args:
            return None  # the lambda does more than forward its parameters
    dotted = func.resolve(parts) or (module_defs.get(parts[0]) if len(parts) == 1 else None)
    if dotted and _is_project_dotted(dotted, project_roots, facts):
        return dotted
    return None


POLICY_ATTR_RE = re.compile(
    r"^(role|roles|perm|perms|permission|permissions|scope|scopes|privilege|privileges|groups|grants|claims|acl|"
    r"is_admin|is_superuser|is_staff|is_owner|is_moderator|is_manager|superuser|admin_roles|allowed_roles|required_roles)$",
    re.I,
)
BOOL_WRAPPERS = frozenset({"bool", "any", "all", "len", "set", "frozenset"})
FORBIDDEN_RAISE_RE = re.compile(r"Permission|Forbidden|Denied|Unauthori[sz]ed|NotAuthori[sz]ed|NotAllowed|AccessDenied|InsufficientScope|StepUp", re.I)
FORBIDDEN_STATUS_RE = re.compile(r"\b403\b|HTTP_403|FORBIDDEN", re.I)
POLICY_CALL_RE = re.compile(
    r"^(has_(role|perm|permission|scope|privilege|access|any_role|all_roles)|can_[a-z_]+|can$|may_[a-z_]+|allows?(_[a-z_]+)?$|allowed(_[a-z_]+)?$|"
    r"is_(admin|owner|superuser|staff|member|moderator|allowed|authorized|authorised)|check_(role|roles|perm|permission|permissions|scope|scopes|access|policy|authorization|authorisation)|"
    r"authori[sz]e[a-z_]*|permitted|enforce[a-z_]*|in_role|in_group|has_authority)",
    re.I,
)


def _policy_names(expr: ast.AST) -> Optional[str]:
    """The privilege-like attribute, key, bare name or predicate read by ``expr``, or None."""
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Attribute) and POLICY_ATTR_RE.match(sub.attr):
            return sub.attr
        if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) and isinstance(sub.slice.value, str) and POLICY_ATTR_RE.match(sub.slice.value):
            return sub.slice.value
        if isinstance(sub, ast.Call):
            parts = dotted_name(sub.func) or []
            if parts and POLICY_CALL_RE.search(parts[-1]):
                return parts[-1] + "()"
        if isinstance(sub, ast.Name) and POLICY_ATTR_RE.match(sub.id):
            return sub.id
    return None


def _forbidden_raise(node: ast.Raise) -> Optional[str]:
    """The exception a ``raise`` denies with, when it is a permission/forbidden kind or an HTTP 403."""
    exc = node.exc
    if exc is None:
        return None
    target = exc.func if isinstance(exc, ast.Call) else exc
    parts = dotted_name(target) or []
    name = parts[-1] if parts else ""
    if name and FORBIDDEN_RAISE_RE.search(name):
        return name
    if isinstance(exc, ast.Call):
        text = ast.unparse(exc) if hasattr(ast, "unparse") else ""
        if name.endswith("HTTPException") and FORBIDDEN_STATUS_RE.search(text):
            return f"{name}(403)"
    return None


def _policy_evidence(func: FunctionInfo) -> List[str]:
    """Structural signs that ``func`` decides on a principal's privileges, as evidence lines. Consulted for dependency
    functions by the route contracts: a dependency named ``require_admin`` whose body shows none of this is a name,
    not a policy. Nested closures and ``__call__`` methods are separate records with their own evidence."""
    out: List[str] = []
    for arg, default in func.param_defaults().items():
        if isinstance(default, ast.Call):
            parts = dotted_name(default.func) or []
            if parts and parts[-1] == "Security" and any(k.arg == "scopes" for k in default.keywords):
                out.append(f"line {default.lineno}: Security(scopes=...) on parameter {arg}")
    for node in func.scope_nodes():
        if isinstance(node, (ast.If, ast.IfExp, ast.Assert)):
            hit = _policy_names(node.test)
            kind = "condition"
        elif isinstance(node, ast.Return) and isinstance(node.value, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            hit = _policy_names(node.value)  # `return user.role == "admin"`, `return bool(principal.roles & required)`
            kind = "predicate"
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
            parts = dotted_name(node.value.func) or []  # `return has_role(user, "admin")`; never `return User(role=user.role)`
            if parts and POLICY_CALL_RE.search(parts[-1]):
                hit = parts[-1] + "()"
            elif parts and parts[-1] in BOOL_WRAPPERS:  # `return bool(user.scopes & required)`, `return any(s in user.roles ...)`
                hit = _policy_names(node.value)
            else:
                hit = None
            kind = "predicate"
        elif isinstance(node, ast.Raise):
            hit = _forbidden_raise(node)  # `raise PermissionDeniedError(...)`, `raise HTTPException(403)`: a denial is a decision
            kind = "denial"
        else:
            continue
        if hit:
            out.append(f"line {node.lineno}: {kind} on {hit}")
    return out


def build_summaries(
    rel: str,
    module: str,
    tree: ast.Module,
    facts: "ProjectFacts",
    local_factories: Dict[str, str],
    project_roots: Optional[Set[str]] = None,
) -> Tuple[List[FunctionRecord], List[ClassRecord]]:
    """Summaries for every function and class in one parsed module."""
    project_roots = project_roots if project_roots is not None else {name.split(".")[0] for name in facts.module_map}
    fastapi_imported = module_imports(tree, ("fastapi",))
    is_package = rel.endswith("__init__.py")
    module_scope, functions = collect_functions(tree, fastapi_imported, module=module, is_package=is_package)
    awaited = awaited_call_ids(tree)
    module_defs: Dict[str, str] = {}  # local name -> module.qualname for top-level defs/classes
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_defs[stmt.name] = f"{module}.{stmt.name}" if module else stmt.name
    module_instances: Dict[str, str] = {}  # module-level `svc = Service(...)`
    module_literals: Set[str] = set()  # module-level dict/list/str constants
    module_resolve = lambda parts: resolve_in_scopes([module_scope], parts)  # noqa: E731
    for stmt in tree.body:
        targets: List[str] = []
        value: Optional[ast.AST] = None
        if isinstance(stmt, ast.Assign):
            targets, value = [t.id for t in stmt.targets if isinstance(t, ast.Name)], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets, value = [stmt.target.id], stmt.value
        if not targets or value is None:
            continue
        if isinstance(value, ast.Call):
            cparts = dotted_name(value.func)
            cdotted = (module_resolve(cparts) or module_defs.get(cparts[0])) if cparts else None
            if cdotted and (cdotted in facts.classes or facts.class_aliases.get(cdotted) or cdotted in module_defs.values()):
                for name in targets:
                    module_instances[name] = facts.class_aliases.get(cdotted, cdotted)
        elif isinstance(value, LITERAL_VALUES) and not (isinstance(value, ast.Constant) and value.value is None):
            module_literals.update(targets)  # `x = None` is a placeholder bound later: stays an object
    classes: Dict[str, ClassRecord] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            key = module_defs[stmt.name]
            bases = []
            is_protocol = is_abstract = False
            for base in stmt.bases:
                parts = dotted_name(base.value if isinstance(base, ast.Subscript) else base)
                if parts:
                    resolved = resolve_in_scopes([module_scope], parts) or module_defs.get(parts[0], ".".join(parts))
                    canon = facts.canonical(resolved) or resolved
                    if canon in PROTOCOL_BASES:
                        is_protocol = True
                        continue
                    if canon in ABSTRACT_BASES:
                        is_abstract = True
                        continue
                    bases.append(resolved)
            for kw in stmt.keywords:
                if kw.arg == "metaclass":
                    mparts = dotted_name(kw.value)
                    if mparts and (resolve_in_scopes([module_scope], mparts) or "").endswith("ABCMeta"):
                        is_abstract = True
            abstract_methods = {
                item.name for item in stmt.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any((resolve_in_scopes([module_scope], dotted_name(d) or []) or "") in ABSTRACT_DECORATORS for d in item.decorator_list)
            }
            classes[key] = ClassRecord(
                key=key, file=rel, bases=bases, is_protocol=is_protocol, is_abstract=is_abstract or bool(abstract_methods),
                abstract_methods=abstract_methods, lineno=stmt.lineno,
            )
            classes[key].attrs = _class_attrs(stmt, functions, facts, classes, local_factories, module_defs)
    records: List[FunctionRecord] = []
    built: Dict[str, Tuple[FunctionRecord, Dict[str, str], Set[str], Set[str]]] = {}  # qualname -> (record, kinds, param_external, param_builtin) for closures

    for func in functions:
        key = f"{module}.{func.qualname}" if module else func.qualname
        params = [a.arg for a in func.params()]
        kinds = function_kinds(func, facts, local_factories)  # declared + local assignments/with
        declared = {p: kinds[p] for p in params if p in kinds}
        own_bound = set(params) | func.scopes[-1].bound
        parent_state = built.get(func.parent.qualname) if func.parent is not None else None
        owner_class = func.class_name  # v1.8: a closure inside a method still talks about the method's `self`
        if owner_class is None and func.parent is not None and "self" not in params:
            ancestor = func.parent
            while ancestor is not None and ancestor.class_name is None:
                ancestor = ancestor.parent
            owner_class = ancestor.class_name if ancestor is not None else None
        cls_key = module_defs.get(owner_class) if owner_class else None
        cls_record = classes.get(cls_key) if cls_key else None
        if cls_record is not None:  # self.<attr> kinds declared in __init__ are visible to every method
            for attr, info in cls_record.attrs.items():
                if info.kind:
                    kinds.setdefault(f"self.{attr}", info.kind)
        rec = FunctionRecord(
            key=key, file=rel, module=module, qualname=func.qualname, name=func.name, is_async=func.is_async,
            lineno=func.lineno, class_name=func.class_name, params=params, declared_kinds=declared,
            decorators=list(func.decorators), is_route=func.route is not None,
        )
        rec.owner_class = owner_class
        if func.route:
            rec.route_label = f"{func.route.method} {func.route.path or ''}".strip()
        if func.class_name and func.parent is None and cls_record is not None:
            cls_record.methods[func.name] = key
        # closures: names the nested function reads but never binds come from the enclosing function
        inherited_external: Set[str] = set()
        inherited_builtin: Set[str] = set()
        if parent_state is not None:
            p_rec, p_kinds, p_external, p_builtin = parent_state
            free = {n.id for n in ast.walk(func.node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - own_bound - set(module_defs)
            for name in free:
                if name in p_kinds and name not in kinds:
                    kinds[name] = p_kinds[name]
                    rec.inherited_kinds[name] = p_kinds[name]
                if name in p_rec.instance_of and name not in rec.instance_of:
                    rec.instance_of[name] = p_rec.instance_of[name]
                if name in p_rec.local_origin:
                    rec.local_origin.setdefault(name, p_rec.local_origin[name])
                if name in p_rec.callback_kinds:
                    rec.callback_kinds.setdefault(name, p_rec.callback_kinds[name])  # send_wrapper awaiting the outer `send`
                if name in p_external:
                    inherited_external.add(name)
                if name in p_builtin:
                    inherited_builtin.add(name)
        rec.retry_decorated = any(d and d.startswith(RETRY_DECORATORS) for d in func.decorators)
        rec.retry_loop = _has_retry_loop(func)

        # parameters: Depends targets, principal-looking names, BackgroundTasks, project instances
        defaults = func.param_defaults()
        param_external: Set[str] = set(inherited_external)  # parameters typed with a resolvable non-project type
        param_builtin: Set[str] = set(inherited_builtin)  # parameters typed with a builtin (str, dict, Exception)
        callable_like: Set[str] = set()  # untyped or Callable-typed parameters (potential callbacks)
        plain_params = [p for p in params if p not in {"self", "cls"}]
        if tuple(plain_params[:3]) == ASGI_TRIPLE:
            rec.asgi_signature = True  # an ASGI application/middleware: receive and send come from the server
        for arg in func.params():
            dep = _depends_target(defaults.get(arg.arg), func.resolve) or _depends_target(arg.annotation, func.resolve)
            if dep:
                _, dotted, parts = dep
                if dotted is None and parts and parts[0] in module_defs:
                    dotted = module_defs[parts[0]] + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                rec.depends[arg.arg] = dotted
            ann = annotation_to_dotted(arg.annotation, func.resolve) or ""
            if not ann and isinstance(arg.annotation, ast.Subscript):  # Callable[[int], int]: generics have no single dotted type
                base_parts = dotted_name(arg.annotation.value)
                base_ann = func.resolve(base_parts) if base_parts else None
                if base_ann and (facts.canonical(base_ann) or base_ann) in CALLABLE_TYPES:
                    ann = base_ann
            if not ann and arg.annotation is not None:  # v1.8: `worker: Worker` naming a class of this module
                same = arg.annotation
                if isinstance(same, ast.Constant) and isinstance(same.value, str):
                    try:
                        same = ast.parse(same.value, mode="eval").body
                    except SyntaxError:
                        same = None
                if isinstance(same, ast.Name) and same.id in module_defs and func.resolve([same.id]) is None:
                    ann = module_defs[same.id]
            if ann and arg.arg not in rec.depends:  # `session: SessionDep` where SessionDep = Annotated[T, Depends(f)]
                alias_target = facts.dependency_aliases.get(ann) or facts.dependency_aliases.get(facts.canonical(ann) or "")
                if alias_target:
                    rec.depends[arg.arg] = alias_target if "." in alias_target else None
            if ann.endswith("BackgroundTasks") or arg.arg == "background_tasks":
                rec.background_task_param = arg.arg
            if any(tok in arg.arg.lower() for tok in PRINCIPAL_PARAM_RE) and arg.arg not in {"self", "cls"}:
                rec.principal_params.append(arg.arg)
            if arg.arg.endswith(("tenant_id", "org_id", "organization_id", "workspace_id", "company_id")) or arg.arg in {"tenant", "organization", "workspace"}:
                rec.tenant_params.append(arg.arg)
            canon_ann = facts.canonical(ann) or ann
            if arg.arg in kinds and arg.arg not in {"self", "cls"}:
                param_external.add(arg.arg)  # a session/client: its methods are library calls
            elif rec.asgi_signature and arg.arg in ASGI_TRIPLE[1:] and arg.arg not in rec.depends:
                callable_like.add(arg.arg)
                rec.callback_kinds[arg.arg] = "asgi"  # receive()/send() are supplied by the ASGI server
            elif ann:
                canon = _canonical_class(ann, classes, facts, project_roots)
                if canon is None and ann.rsplit(".", 1)[-1] in module_defs and module_defs[ann.rsplit(".", 1)[-1]] in classes:
                    canon = module_defs[ann.rsplit(".", 1)[-1]]
                if canon:
                    rec.instance_of[arg.arg] = canon
                elif canon_ann in ASGI_CALLBACK_TYPES:
                    if canon_ann.endswith(("Scope", "Message")):
                        param_external.add(arg.arg)  # dict-like values handed by the server
                    else:
                        callable_like.add(arg.arg)
                        rec.callback_kinds[arg.arg] = "asgi"
                elif canon_ann in CALLABLE_TYPES or canon_ann.rsplit(".", 1)[-1].endswith(CALLABLE_NAME_HINTS):
                    callable_like.add(arg.arg)
                    rec.callback_kinds[arg.arg] = "callable"
                elif not _is_project_dotted(ann, project_roots, facts):
                    param_external.add(arg.arg)
            elif _builtin_annotation(arg.annotation, func.resolve):
                param_builtin.add(arg.arg)
            elif arg.annotation is None and arg.arg not in {"self", "cls"} and arg.arg not in rec.depends:
                callable_like.add(arg.arg)
                rec.callback_kinds[arg.arg] = "untyped"

        # return / yield kind for dependency functions; return class for factories of project objects
        returns = getattr(func.node, "returns", None)
        rk = _unwrap_return_kind(returns, func.resolve, facts)
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
        if returns is not None:
            rdotted = annotation_to_dotted(returns, func.resolve)
            rclass = _canonical_class(rdotted, classes, facts, project_roots) if rdotted else None
            if rclass is None and rdotted and rdotted.rsplit(".", 1)[-1] in module_defs and module_defs[rdotted.rsplit(".", 1)[-1]] in classes:
                rclass = module_defs[rdotted.rsplit(".", 1)[-1]]
            if rclass is None and isinstance(returns, ast.Constant) and isinstance(returns.value, str) and returns.value in module_defs:
                rclass = module_defs[returns.value] if module_defs[returns.value] in classes else None
            rec.returns_class = rclass
        if returns is not None:
            rec.returns_dotted = facts.canonical(rdotted) or rdotted
            rec.returns_builtin = _builtin_annotation(returns, func.resolve) is not None
            rec.returns_none = isinstance(returns, ast.Constant) and returns.value is None
        for node in func.scope_nodes():  # request.state.user = ... / scope["state"]["user"] = ...
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            for target in targets:
                chain = dotted_name(target.value) if isinstance(target, ast.Attribute) else None
                if chain and len(chain) >= 2 and chain[1] == "state":
                    rec.writes_request_state = True
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Subscript) and isinstance(target.value.slice, ast.Constant) and target.value.slice.value == "state":
                    rec.writes_request_state = True
        rec.policy_evidence = _policy_evidence(func)
        if rec.returns_class is None:
            for node in func.scope_nodes():
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                    cparts = dotted_name(node.value.func)
                    if cparts:
                        cdotted = func.resolve(cparts) or module_defs.get(cparts[0])
                        canon = _canonical_class(cdotted, classes, facts)
                        if canon:
                            rec.returns_class = canon
                            break
                        if cparts == ["cls"] and func.class_name and cls_key in classes:
                            rec.returns_class = cls_key
                            break

        # body scan: calls, blocking, writes, factories, tenant context, idempotency, instances
        texts = " ".join(_string_constants(func.node)).lower()
        rec.sets_tenant_context = any(m in texts for m in TENANT_CONTEXT_MARKERS)
        rec.idempotency_signal = any(m in texts for m in IDEMPOTENCY_MARKERS) or any(
            any(m in a.arg.lower() for m in ("idempot", "dedup")) for a in func.params()
        )
        loop_vars: Set[str] = set()
        comp_vars: Set[str] = set()
        nested_defs = {n.name for n in func.scope_nodes() if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(func.node):  # comprehension targets live in their own scope but read like locals
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                for gen in node.generators:
                    comp_vars |= {n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
        for node in func.scope_nodes():
            if isinstance(node, (ast.For, ast.AsyncFor)):
                loop_vars |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            # locals bound to project instances, project call results, or external values
            binding_targets: List[str] = []
            value: Optional[ast.AST] = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                binding_targets, value = [node.targets[0].id], node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], (ast.Tuple, ast.List)):
                targets = [e.id for e in node.targets[0].elts if isinstance(e, ast.Name)]
                values = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) and len(node.value.elts) == len(node.targets[0].elts) else None
                if values is not None:  # a, b = f(), g(): pairwise
                    for elt, val in zip(node.targets[0].elts, values):
                        if isinstance(elt, ast.Name):
                            _bind_origin(rec, elt.id, val, func, module_defs, classes, facts, project_roots, kinds, param_external, param_builtin, cls_record)
                else:  # a, b = call(): every target shares the call's origin class (external/builtin), never a project class
                    inner = node.value.value if isinstance(node.value, ast.Await) else node.value
                    origin = _call_origin_class(inner, func, module_defs, facts, project_roots, kinds, param_external, param_builtin, rec, cls_record)
                    for name in targets:
                        if origin in {"external", "builtin"} and name not in kinds:
                            rec.local_origin[name] = (origin, "")
                        elif origin is None:
                            rec.local_origin.setdefault(name, ("unpack", ""))
                continue
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                binding_targets, value = [node.target.id], node.value
                adotted = annotation_to_dotted(node.annotation, func.resolve)
                acls = _canonical_class(adotted, classes, facts, project_roots)
                if acls:
                    rec.instance_of[node.target.id] = acls
                elif adotted and not _is_project_dotted(adotted, project_roots, facts):
                    rec.local_origin[node.target.id] = ("external", "")
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                        self_bind_value = item.context_expr
                        cparts = dotted_name(self_bind_value.func)
                        if cparts:
                            cdotted = func.resolve(cparts) or module_defs.get(cparts[0])
                            canon = _canonical_class(cdotted, classes, facts, project_roots)
                            if canon:
                                rec.instance_of[item.optional_vars.id] = canon
                            elif cdotted and not _is_project_dotted(cdotted, project_roots, facts) and item.optional_vars.id not in kinds:
                                rec.local_origin[item.optional_vars.id] = ("external", "")
                            elif cdotted is None and len(cparts) >= 2 and item.optional_vars.id not in kinds:
                                root = cparts[0]  # async with engine.connect() as conn / self._client.stream() as r
                                if root in param_external or rec.local_origin.get(root, ("", ""))[0] == "external" or kinds.get(root) or (root == "self" and f"self.{cparts[1]}" in kinds):
                                    rec.local_origin[item.optional_vars.id] = ("external", "")
            if binding_targets and value is not None:
                _bind_origin(rec, binding_targets[0], value, func, module_defs, classes, facts, project_roots, kinds, param_external, param_builtin, cls_record)
            if not isinstance(node, ast.Call):
                continue
            call = node
            parts = dotted_name(call.func)
            site = CallSite(
                lineno=call.lineno, col=call.col_offset, target=None, status="dynamic",
                receiver=receiver_name(call), attr=call.func.attr if isinstance(call.func, ast.Attribute) else None,
                awaited=id(call) in awaited,
            )
            for i, arg in enumerate(call.args):
                if isinstance(arg, ast.Name):
                    site.arg_names.append((i, arg.id))
                elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                    site.arg_names.append((i, arg.value.id))
                elif isinstance(arg, ast.Attribute):
                    aparts = dotted_name(arg)
                    if aparts:
                        site.arg_roots.append((i, aparts[0]))
                        if aparts[0] in {"self", "cls"} and len(aparts) == 2 and cls_key:
                            site.arg_callables.append((i, f"{cls_key}.{aparts[1]}"))
            for kw in call.keywords:
                if kw.arg and isinstance(kw.value, ast.Name):
                    site.arg_names.append((kw.arg, kw.value.id))
                elif kw.arg and isinstance(kw.value, ast.Attribute):
                    aparts = dotted_name(kw.value)
                    if aparts:
                        site.arg_roots.append((kw.arg, aparts[0]))
            for position, name in list(site.arg_names):  # functions handed as arguments (callbacks)
                if name in params or name in kinds or name in rec.instance_of:
                    continue
                cdotted = func.resolve([name])
                if cdotted is None and name in module_defs:
                    cdotted = module_defs[name]
                if cdotted and _is_project_dotted(cdotted, project_roots, facts):
                    site.arg_callables.append((position, cdotted))
            for i, arg in enumerate(call.args):
                if isinstance(arg, ast.Lambda):  # lambda v: target(v): a wrapper with one verifiable destination
                    target = _lambda_target(arg, func, module_defs, facts, project_roots)
                    if target:
                        site.arg_callables.append((i, target))
                elif isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.value.id in rec.instance_of:
                    site.arg_callables.append((i, f"{rec.instance_of[arg.value.id]}.{arg.attr}"))  # worker.run handed as a callback
            for kw in call.keywords:
                if isinstance(kw.value, ast.Lambda) and kw.arg:
                    target = _lambda_target(kw.value, func, module_defs, facts, project_roots)
                    if target:
                        site.arg_callables.append((kw.arg, target))
            dotted = func.resolve(parts) if parts else None
            if dotted:
                site.target, site.status = dotted, "candidate"
            elif parts:
                root = parts[0]
                root_is_local = root in params or any(root in sc.bound for sc in func.scopes[1:])  # parameter or local: shadows module names
                if root in {"self", "cls"} and owner_class and len(parts) == 2:
                    site.target = f"{module_defs.get(func.class_name, func.class_name)}.{parts[1]}"
                    site.status, site.has_receiver = "candidate", True
                elif root in {"self", "cls"} and owner_class and len(parts) == 3:
                    rec.self_attr_receivers.add(parts[1])
                    if f"self.{parts[1]}" in kinds:
                        site.status, site.target = "external", f"{kinds[f'self.{parts[1]}']}.{parts[2]}"
                    else:
                        site.status, site.hint, site.has_receiver = "hint", ("attr", parts[1], parts[2]), True
                elif root in {"self", "cls"} and owner_class and len(parts) == 4:
                    site.status, site.hint, site.has_receiver = "hint", ("attr2", parts[1], parts[2], parts[3]), True
                elif root in {"self", "cls"} and owner_class:
                    site.origin = "attribute_chain"
                elif root_is_local:
                    origin_kind = rec.local_origin.get(root, ("", ""))[0]
                    if root in rec.instance_of and len(parts) == 2:
                        site.target = f"{rec.instance_of[root]}.{parts[1]}"
                        site.status, site.has_receiver = "candidate", True
                    elif root in rec.instance_of and len(parts) == 3:
                        site.status, site.hint, site.has_receiver = "hint", ("iattr", rec.instance_of[root], parts[1], parts[2]), True
                    elif len(parts) == 1 and root in callable_like and root in params:
                        site.status, site.hint = "hint", ("callback", root, rec.callback_kinds.get(root, "untyped"))
                    elif len(parts) == 2 and (root in param_builtin or origin_kind == "builtin"):
                        site.status = "builtin"  # str/dict/list methods on a builtin-typed value
                    elif len(parts) == 2 and (root in param_external or kinds.get(root) or origin_kind == "external"):
                        site.status, site.target = "external", f"{kinds.get(root) or 'external'}.{parts[1]}"
                    elif len(parts) == 2 and origin_kind == "ret":
                        site.status, site.hint, site.has_receiver = "hint", ("ret", rec.local_origin[root][1], parts[1]), True
                    elif len(parts) == 1 and root in rec.callback_kinds and (root in params or root not in own_bound):
                        site.status, site.hint = "hint", ("callback", root, rec.callback_kinds[root])
                    else:
                        if len(parts) == 1 and root in nested_defs:
                            site.origin = "nested_function"  # a def inside this function, called locally: not followed (documented limit)
                        elif root in loop_vars:
                            site.origin = "loop_target"
                        elif root in comp_vars:
                            site.origin = "comprehension"
                        elif origin_kind == "unpack":
                            site.origin = "unpack"
                        elif origin_kind == "callback_result":
                            site.origin = "callback_result"
                        elif root in params:
                            site.origin = "untyped_param"
                        elif parent_state is not None and root not in own_bound:
                            site.origin = "closure_unknown"
                        else:
                            site.origin = "call_result_unknown" if origin_kind == "ret" else "local_unknown"
                elif root in comp_vars and len(parts) == 2:
                    site.origin = "comprehension"
                elif parent_state is not None and root not in own_bound and root not in module_scope.bound and root not in module_defs and root not in module_scope.imports and root not in BUILTIN_NAMES:
                    site.origin = "closure_unknown"  # read from an enclosing scope the parent could not type either
                elif root in module_defs and len(parts) <= 2:
                    site.target = module_defs[root] + ("." + parts[1] if len(parts) == 2 else "")
                    site.status = "candidate"
                elif root in module_instances and len(parts) == 2:
                    site.target = f"{module_instances[root]}.{parts[1]}"
                    site.status, site.has_receiver = "candidate", True
                elif root in module_literals and len(parts) == 2:
                    site.status = "builtin"
                elif root in module_scope.bound and len(parts) >= 2:
                    site.status = "object"  # module-level object of this module (router, factory, constant, context var)
                elif len(parts) == 1 and root in BUILTIN_NAMES and not any(root in s.bound or root in s.imports for s in func.scopes):
                    site.status = "builtin"
            elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, (ast.Await, ast.Call)):
                inner = call.func.value.value if isinstance(call.func.value, ast.Await) else call.func.value
                if isinstance(inner, ast.Call):
                    rk_inner = receiver_kind(inner, kinds)
                    iparts = dotted_name(inner.func)
                    idotted = func.resolve(iparts) if iparts else None
                    iroot = iparts[0] if iparts else None
                    if rk_inner or (idotted and not _is_project_dotted(idotted, project_roots, facts)) or (
                        iroot and (iroot in param_external or rec.local_origin.get(iroot, ("", ""))[0] in {"external", "builtin"})
                    ):
                        site.status = "external"  # (await session.execute(...)).scalars()
                    elif iroot and len(iparts) == 1 and iroot in BUILTIN_NAMES and not any(iroot in s.bound or iroot in s.imports for s in func.scopes):
                        site.status = "builtin"  # ", ".join(...) style chains on builtin results
            elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Constant):
                site.status = "builtin"  # ", ".join(items)
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
            if (dotted and dotted.startswith(HTTP_MODULES) and dotted.rsplit(".", 1)[-1] in NETWORK_VERBS) or dotted in SLEEP_CALLS:
                rec.network_calls.append((call.lineno, dotted))
            elif rk_recv in CLIENT_KINDS and site.attr in NETWORK_VERBS:
                rec.network_calls.append((call.lineno, f"{site.receiver}.{site.attr}"))
            elif rk_recv in SESSION_KINDS and site.attr in DB_WRITE_METHODS:
                rec.writes.append((call.lineno, "db"))
            elif site.attr in {"commit", "add", "add_all"} and site.receiver in {"session", "db", "db_session"}:
                rec.writes.append((call.lineno, "db"))
            if dotted in FACTORY_KINDS or kind_of_call(call, func.resolve, facts, local_factories) in SESSION_KINDS:
                rec.creates_session = True
            elif site.receiver and site.receiver.startswith("self.") and cls_record is not None:
                info = cls_record.attrs.get(site.receiver[5:])
                if info is not None and info.factory and not parts[2:3] == ["begin_nested"]:
                    rec.creates_session = True
        records.append(rec)
        built[func.qualname] = (rec, kinds, param_external, param_builtin)
    return records, list(classes.values())
