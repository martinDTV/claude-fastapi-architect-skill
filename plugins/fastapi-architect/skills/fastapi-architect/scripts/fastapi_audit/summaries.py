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
CALLABLE_TYPES = frozenset({"typing.Callable", "collections.abc.Callable", "typing.Awaitable", "collections.abc.Awaitable", "typing.Coroutine"})
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


def _class_attrs(cls: ast.ClassDef, functions: List[FunctionInfo], facts: "ProjectFacts", classes: Dict[str, ClassRecord], local_factories: Dict[str, str]) -> Dict[str, AttrInfo]:
    """Instance attributes with a visible type: ``self.x = <typed param>``, ``self.x = Cls()``,
    ``self.x: T = ...`` in ``__init__`` (first assignment wins), and class-level ``x: T`` annotations."""
    attrs: Dict[str, AttrInfo] = {}
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
                dotted = annotation_to_dotted(annotation, init.resolve) or _builtin_annotation(annotation, init.resolve)
                attrs[name] = AttrInfo(type=dotted, kind=kind_of_type(dotted, facts), source="init-annassign")
                continue
            if isinstance(value, ast.Name) and value.id in params:
                ann = params[value.id]
                dotted = (annotation_to_dotted(ann, init.resolve) or _builtin_annotation(ann, init.resolve)) if ann is not None else None
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
                dotted = (annotation_to_dotted(ann, init.resolve) or _builtin_annotation(ann, init.resolve)) if ann is not None else None
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
            dotted = annotation_to_dotted(item.annotation, resolve) or _builtin_annotation(item.annotation, resolve)
            attrs[item.target.id] = AttrInfo(type=dotted, kind=kind_of_type(dotted, facts), source="classattr")
    return attrs


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
            classes[key].attrs = _class_attrs(stmt, functions, facts, classes, local_factories)
    records: List[FunctionRecord] = []

    for func in functions:
        key = f"{module}.{func.qualname}" if module else func.qualname
        params = [a.arg for a in func.params()]
        kinds = function_kinds(func, facts, local_factories)  # declared + local assignments/with
        declared = {p: kinds[p] for p in params if p in kinds}
        cls_key = module_defs.get(func.class_name) if func.class_name else None
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
        if func.route:
            rec.route_label = f"{func.route.method} {func.route.path or ''}".strip()
        if func.class_name and func.parent is None and cls_record is not None:
            cls_record.methods[func.name] = key
        rec.retry_decorated = any(d and d.startswith(RETRY_DECORATORS) for d in func.decorators)
        rec.retry_loop = _has_retry_loop(func)

        # parameters: Depends targets, principal-looking names, BackgroundTasks, project instances
        defaults = func.param_defaults()
        param_external: Set[str] = set()  # parameters typed with a resolvable non-project type
        param_builtin: Set[str] = set()  # parameters typed with a builtin (str, dict, Exception)
        callable_like: Set[str] = set()  # untyped or Callable-typed parameters (potential callbacks)
        for arg in func.params():
            dep = _depends_target(defaults.get(arg.arg), func.resolve) or _depends_target(arg.annotation, func.resolve)
            if dep:
                _, dotted, parts = dep
                if dotted is None and parts and parts[0] in module_defs:
                    dotted = module_defs[parts[0]] + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                rec.depends[arg.arg] = dotted
            ann = annotation_to_dotted(arg.annotation, func.resolve) or ""
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
            if arg.arg in kinds and arg.arg not in {"self", "cls"}:
                param_external.add(arg.arg)  # a session/client: its methods are library calls
            elif ann:
                canon = _canonical_class(ann, classes, facts, project_roots)
                if canon is None and ann.rsplit(".", 1)[-1] in module_defs and module_defs[ann.rsplit(".", 1)[-1]] in classes:
                    canon = module_defs[ann.rsplit(".", 1)[-1]]
                if canon:
                    rec.instance_of[arg.arg] = canon
                elif (facts.canonical(ann) or ann) in CALLABLE_TYPES:
                    callable_like.add(arg.arg)
                elif not _is_project_dotted(ann, project_roots, facts):
                    param_external.add(arg.arg)
            elif _builtin_annotation(arg.annotation, func.resolve):
                param_builtin.add(arg.arg)
            elif arg.annotation is None and arg.arg not in {"self", "cls"} and arg.arg not in rec.depends:
                callable_like.add(arg.arg)

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
        for node in func.scope_nodes():
            # locals bound to project instances, project call results, or external values
            binding_targets: List[str] = []
            value: Optional[ast.AST] = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                binding_targets, value = [node.targets[0].id], node.value
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
                inner = value.value if isinstance(value, ast.Await) else value
                name0 = binding_targets[0]
                if isinstance(inner, LITERAL_VALUES) and name0 not in kinds and not (isinstance(inner, ast.Constant) and inner.value is None):
                    rec.local_origin[name0] = ("builtin", "")
                elif isinstance(inner, (ast.Attribute, ast.Subscript)) and name0 not in kinds:
                    aparts = dotted_name(inner.value if isinstance(inner, ast.Subscript) else inner)
                    root = aparts[0] if aparts else None
                    if root and (root in param_external or root in param_builtin or rec.local_origin.get(root, ("", ""))[0] in {"external", "builtin"} or kinds.get(root)):
                        rec.local_origin[name0] = ("external", "")
                    elif root == "self" and aparts and len(aparts) >= 2 and cls_record is not None and (cls_record.attrs.get(aparts[1]) is not None) and (cls_record.attrs[aparts[1]].kind or (cls_record.attrs[aparts[1]].type or "").startswith("builtins.")):
                        rec.local_origin[name0] = ("external", "")
                if isinstance(inner, ast.Call):
                    cparts = dotted_name(inner.func)
                    cdotted = (func.resolve(cparts) or module_defs.get(cparts[0])) if cparts else None
                    canon = _canonical_class(cdotted, classes, facts, project_roots)
                    name = binding_targets[0]
                    if canon:
                        rec.instance_of[name] = canon
                    elif cdotted and _is_project_dotted(cdotted, project_roots, facts):
                        rec.local_origin[name] = ("ret", cdotted)
                    elif cparts and len(cparts) == 1 and cparts[0] in module_defs:
                        rec.local_origin[name] = ("ret", module_defs[cparts[0]])
                    elif cdotted and name not in kinds:
                        rec.local_origin[name] = ("external", "")
                    elif cparts and len(cparts) == 1 and cparts[0] in BUILTIN_NAMES and name not in kinds and not any(cparts[0] in sc.bound or cparts[0] in sc.imports for sc in func.scopes):
                        rec.local_origin[name] = ("builtin", "")  # x = dict(...), s = str(...)
                    elif cparts and len(cparts) >= 2 and name not in kinds:
                        root = cparts[0]
                        if root in param_external or root in param_builtin or rec.local_origin.get(root, ("", ""))[0] in {"external", "builtin"} or kinds.get(root) or (root == "self" and f"self.{cparts[1]}" in kinds):
                            rec.local_origin[name] = ("external", "")
                    elif isinstance(inner.func, ast.Attribute) and isinstance(inner.func.value, (ast.Await, ast.Call)) and name not in kinds:
                        rec.local_origin[name] = ("external", "")  # (await session.execute(...)).scalars()
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
            dotted = func.resolve(parts) if parts else None
            if dotted:
                site.target, site.status = dotted, "candidate"
            elif parts:
                root = parts[0]
                root_is_local = root in params or any(root in sc.bound for sc in func.scopes[1:])  # parameter or local: shadows module names
                if root in {"self", "cls"} and func.class_name and len(parts) == 2:
                    site.target = f"{module_defs.get(func.class_name, func.class_name)}.{parts[1]}"
                    site.status, site.has_receiver = "candidate", True
                elif root in {"self", "cls"} and func.class_name and len(parts) == 3:
                    rec.self_attr_receivers.add(parts[1])
                    if f"self.{parts[1]}" in kinds:
                        site.status, site.target = "external", f"{kinds[f'self.{parts[1]}']}.{parts[2]}"
                    else:
                        site.status, site.hint, site.has_receiver = "hint", ("attr", parts[1], parts[2]), True
                elif root_is_local:
                    if root in rec.instance_of and len(parts) == 2:
                        site.target = f"{rec.instance_of[root]}.{parts[1]}"
                        site.status, site.has_receiver = "candidate", True
                    elif len(parts) == 1 and root in callable_like and root in params:
                        site.status, site.hint = "hint", ("callback", root)
                    elif len(parts) == 2 and (root in param_builtin or rec.local_origin.get(root, ("", ""))[0] == "builtin"):
                        site.status = "builtin"  # str/dict/list methods on a builtin-typed value
                    elif len(parts) == 2 and (root in param_external or kinds.get(root) or rec.local_origin.get(root, ("", ""))[0] == "external"):
                        site.status, site.target = "external", f"{kinds.get(root) or 'external'}.{parts[1]}"
                    elif len(parts) == 2 and rec.local_origin.get(root, ("", ""))[0] == "ret":
                        site.status, site.hint, site.has_receiver = "hint", ("ret", rec.local_origin[root][1], parts[1]), True
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
    return records, list(classes.values())
