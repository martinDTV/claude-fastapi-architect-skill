"""Conservative symbol resolution: scopes, import aliases, routes, annotations.

Nothing here executes or imports inspected code. Resolution answers "which
fully qualified name does this call/annotation refer to, if that can be told
from imports and local bindings alone"; when it cannot, it answers ``None``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

ROUTE_DECORATOR_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}
)
TYPING_ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})
TYPING_OPTIONAL = frozenset({"typing.Optional", "typing.Union", "typing_extensions.Optional"})


def dotted_name(node: ast.AST) -> Optional[List[str]]:
    """Return ['a', 'b', 'c'] for ``a.b.c`` when the root is a plain name."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return parts
    return None


@dataclass
class Scope:
    name: str
    is_async: bool
    imports: Dict[str, Optional[str]] = field(default_factory=dict)
    bound: Set[str] = field(default_factory=set)


def collect_scope(
    body: Iterable[ast.AST], args: Optional[ast.arguments] = None
) -> Tuple[Dict[str, Optional[str]], Set[str]]:
    """Collect imports and locally bound names of one scope.

    Nested function, class and lambda bodies are not descended into: they are
    separate scopes. A local name imported from two different modules in the
    same scope is recorded as ambiguous (``None``) and never matched.
    """
    imports: Dict[str, Optional[str]] = {}
    bound: Set[str] = set()
    if args is not None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            bound.add(arg.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)

    def record(local: str, target: str) -> None:
        if local in imports and imports[local] != target:
            imports[local] = None
        elif local not in imports:
            imports[local] = target

    stack: List[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    record(alias.asname, alias.name)
                else:
                    head = alias.name.split(".")[0]
                    record(head, head)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if node.level == 0 and node.module:
                    record(local, node.module + "." + alias.name)
                else:
                    bound.add(local)  # relative import: origin unknown
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return imports, bound


def resolve_in_scopes(scopes: Sequence[Scope], parts: Sequence[str]) -> Optional[str]:
    """Resolve a dotted reference through a scope chain (innermost last)."""
    if not parts:
        return None
    root = parts[0]
    for scope in reversed(scopes):
        if root in scope.bound:
            return None  # shadowed by a parameter, assignment or local def
        if root in scope.imports:
            target = scope.imports[root]
            if target is None:
                return None  # ambiguous import
            return ".".join([target, *parts[1:]])
    return None  # builtin, star import or undefined: not tracked


def iter_scope_nodes(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Depth-first over ``nodes`` without entering nested function/class/lambda bodies.

    The nested definition node itself is yielded (so decorators/names can be
    inspected) but its body is not traversed.
    """
    stack: List[ast.AST] = list(nodes)
    stack.reverse()
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        children = list(ast.iter_child_nodes(node))
        children.reverse()
        stack.extend(children)


def module_imports(tree: ast.AST, prefixes: Iterable[str]) -> bool:
    """True when the module imports any of ``prefixes`` (exact or dotted prefix)."""
    prefixes = tuple(prefixes)

    def hit(name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in prefixes)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(hit(a.name) for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if hit(node.module):
                return True
    return False


def module_imports_fastapi(tree: ast.AST) -> bool:
    return module_imports(tree, ("fastapi",))


def awaited_call_ids(tree: ast.AST) -> Set[int]:
    """ids of Call nodes that are the direct operand of an ``await``."""
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            ids.add(id(node.value))
    return ids


@dataclass
class RouteInfo:
    method: str
    path: Optional[str]
    receiver: str  # dotted receiver of the decorator, e.g. "router" or "self.router"
    keywords: Set[str]
    decorator: ast.Call
    has_dependencies: bool
    fastapi_imported: bool


@dataclass
class FunctionInfo:
    node: ast.AST  # FunctionDef | AsyncFunctionDef
    name: str
    qualname: str
    is_async: bool
    scopes: List[Scope]  # module scope ... own scope
    class_name: Optional[str]
    route: Optional[RouteInfo]
    decorators: List[Optional[str]]  # resolved dotted names (None when unresolvable)
    parent: Optional["FunctionInfo"] = None
    calls_cache: Optional[List[ast.Call]] = None  # filled lazily by rules.async_rules.iter_function_calls

    @property
    def body(self) -> List[ast.stmt]:
        return list(getattr(self.node, "body", []))

    @property
    def args(self) -> ast.arguments:
        return getattr(self.node, "args")

    @property
    def lineno(self) -> int:
        return int(getattr(self.node, "lineno", 1))

    def resolve(self, parts: Sequence[str]) -> Optional[str]:
        return resolve_in_scopes(self.scopes, parts)

    def resolve_node(self, node: ast.AST) -> Optional[str]:
        parts = dotted_name(node)
        return self.resolve(parts) if parts else None

    def params(self) -> List[ast.arg]:
        a = self.args
        return [*a.posonlyargs, *a.args, *a.kwonlyargs]

    def param_defaults(self) -> Dict[str, Optional[ast.AST]]:
        """Map parameter name -> default node (None when there is none)."""
        a = self.args
        positional = [*a.posonlyargs, *a.args]
        defaults: Dict[str, Optional[ast.AST]] = {p.arg: None for p in positional}
        for arg, default in zip(positional[len(positional) - len(a.defaults) :], a.defaults):
            defaults[arg.arg] = default
        for arg, default in zip(a.kwonlyargs, a.kw_defaults):
            defaults[arg.arg] = default
        return defaults

    def scope_nodes(self) -> Iterator[ast.AST]:
        return iter_scope_nodes(self.body)


def detect_route(
    node: ast.AST, scopes: Sequence[Scope], fastapi_imported: bool
) -> Optional[RouteInfo]:
    for decorator in getattr(node, "decorator_list", []):
        if not isinstance(decorator, ast.Call):
            continue
        parts = dotted_name(decorator.func)
        if not parts or len(parts) < 2 or parts[-1] not in ROUTE_DECORATOR_METHODS:
            continue
        receiver = parts[-2]
        if not (fastapi_imported or "router" in receiver.lower()):
            continue
        keywords = {kw.arg for kw in decorator.keywords if kw.arg}
        path: Optional[str] = None
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            if isinstance(decorator.args[0].value, str):
                path = decorator.args[0].value
        elif "path" in keywords:
            for kw in decorator.keywords:
                if kw.arg == "path" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        path = kw.value.value
        return RouteInfo(
            method=parts[-1].upper(),
            path=path,
            receiver=".".join(parts[:-1]),
            keywords=keywords,
            decorator=decorator,
            has_dependencies="dependencies" in keywords,
            fastapi_imported=fastapi_imported,
        )
    return None


def collect_functions(tree: ast.Module, fastapi_imported: bool) -> Tuple[Scope, List[FunctionInfo]]:
    """Return the module scope and every function with its scope chain, in source order."""
    imports, bound = collect_scope(tree.body)
    module_scope = Scope("<module>", False, imports, bound)
    functions: List[FunctionInfo] = []

    def walk(nodes: Iterable[ast.AST], scopes: List[Scope], class_name: Optional[str], parent: Optional[FunctionInfo]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f_imports, f_bound = collect_scope(node.body, node.args)
                is_async = isinstance(node, ast.AsyncFunctionDef)
                own = Scope(node.name, is_async, f_imports, f_bound)
                chain = [*scopes, own]
                names = [s.name for s in chain[1:] if not s.name.startswith("<")]
                decorators = [resolve_in_scopes(scopes, dotted_name(d.func if isinstance(d, ast.Call) else d) or []) for d in node.decorator_list]
                info = FunctionInfo(
                    node=node,
                    name=node.name,
                    qualname=".".join(names),
                    is_async=is_async,
                    scopes=chain,
                    class_name=class_name,
                    route=detect_route(node, scopes, fastapi_imported),
                    decorators=decorators,
                    parent=parent,
                )
                functions.append(info)
                walk(node.body, chain, None, info)
            elif isinstance(node, ast.ClassDef):
                c_imports, c_bound = collect_scope(node.body)
                chain = [*scopes, Scope(node.name, False, c_imports, c_bound)]
                walk(node.body, chain, node.name, parent)
            else:
                # statements that may contain nested defs (if/try/with/for/while)
                nested = [c for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)]
                if nested:
                    walk(nested, scopes, class_name, parent)

    walk(tree.body, [module_scope], None, None)
    return module_scope, functions


def annotation_to_dotted(node: Optional[ast.AST], resolve) -> Optional[str]:
    """Resolve an annotation to a dotted type name, unwrapping Annotated/Optional/Union/|.

    ``resolve`` maps a list of name parts to a dotted string or None.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            inner = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
        return annotation_to_dotted(inner, resolve)
    if isinstance(node, (ast.Name, ast.Attribute)):
        parts = dotted_name(node)
        return resolve(parts) if parts else None
    if isinstance(node, ast.Subscript):
        base_parts = dotted_name(node.value)
        base = resolve(base_parts) if base_parts else None
        inner = node.slice
        if isinstance(inner, ast.Index):  # Python 3.8 compatibility
            inner = inner.value  # type: ignore[attr-defined]
        if base in TYPING_ANNOTATED or base in TYPING_OPTIONAL:
            if isinstance(inner, ast.Tuple) and inner.elts:
                for elt in inner.elts:
                    if isinstance(elt, ast.Constant) and elt.value is None:
                        continue
                    return annotation_to_dotted(elt, resolve)
                return None
            return annotation_to_dotted(inner, resolve)
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and side.value is None:
                continue
            dotted = annotation_to_dotted(side, resolve)
            if dotted:
                return dotted
        return None
    return None


def call_dotted(call: ast.Call, resolve) -> Optional[str]:
    parts = dotted_name(call.func)
    return resolve(parts) if parts else None


def constant_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
