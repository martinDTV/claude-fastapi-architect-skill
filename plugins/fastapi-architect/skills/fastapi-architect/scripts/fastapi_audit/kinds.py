"""Session/client "kinds": what a name is likely to be, from annotations and factories.

Kinds are only assigned when the evidence is explicit: a resolvable
annotation, a constructor call, or a call to a factory bound at module level
(sessionmaker / async_sessionmaker / scoped_session). Everything else is
``None`` (unknown), and rules treat unknown conservatively.
"""

from __future__ import annotations

import ast
from typing import Dict, Optional, TYPE_CHECKING

from .resolver import FunctionInfo, annotation_to_dotted, dotted_name, iter_scope_nodes

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ProjectFacts

KIND_SESSION = "Session"
KIND_ASYNC_SESSION = "AsyncSession"
KIND_CLIENT = "Client"  # sync HTTP client
KIND_ASYNC_CLIENT = "AsyncClient"

SESSION_KINDS = frozenset({KIND_SESSION, KIND_ASYNC_SESSION})
CLIENT_KINDS = frozenset({KIND_CLIENT, KIND_ASYNC_CLIENT})

TYPE_KINDS: Dict[str, str] = {
    "sqlalchemy.orm.Session": KIND_SESSION,
    "sqlalchemy.orm.session.Session": KIND_SESSION,
    "sqlalchemy.ext.asyncio.AsyncSession": KIND_ASYNC_SESSION,
    "sqlalchemy.ext.asyncio.session.AsyncSession": KIND_ASYNC_SESSION,
    "sqlmodel.Session": KIND_SESSION,
    "sqlmodel.ext.asyncio.session.AsyncSession": KIND_ASYNC_SESSION,
    "httpx.AsyncClient": KIND_ASYNC_CLIENT,
    "httpx.Client": KIND_CLIENT,
    "requests.Session": KIND_CLIENT,
    "requests.sessions.Session": KIND_CLIENT,
}

# factory callable -> kind of the sessions it produces
FACTORY_KINDS: Dict[str, str] = {
    "sqlalchemy.orm.sessionmaker": KIND_SESSION,
    "sqlalchemy.orm.session.sessionmaker": KIND_SESSION,
    "sqlalchemy.orm.scoped_session": KIND_SESSION,
    "sqlalchemy.orm.scoping.scoped_session": KIND_SESSION,
    "sqlalchemy.ext.asyncio.async_sessionmaker": KIND_ASYNC_SESSION,
    "sqlalchemy.ext.asyncio.session.async_sessionmaker": KIND_ASYNC_SESSION,
    "sqlalchemy.ext.asyncio.async_scoped_session": KIND_ASYNC_SESSION,
}

ENGINE_FACTORIES = frozenset(
    {
        "sqlalchemy.create_engine",
        "sqlalchemy.engine.create_engine",
        "sqlalchemy.engine.create.create_engine",
        "sqlalchemy.ext.asyncio.create_async_engine",
        "sqlalchemy.ext.asyncio.engine.create_async_engine",
    }
)

# AsyncSession methods that return coroutines (must be awaited)
ASYNC_SESSION_COROUTINES = frozenset(
    {
        "execute",
        "scalar",
        "scalars",
        "get",
        "get_one",
        "commit",
        "rollback",
        "flush",
        "refresh",
        "merge",
        "delete",
        "close",
        "run_sync",
        "stream",
        "stream_scalars",
        "connection",
        "invalidate",
        "reset",
    }
)
# sync Session methods that perform database I/O
SYNC_SESSION_IO = frozenset(
    {
        "execute",
        "scalar",
        "scalars",
        "get",
        "get_one",
        "commit",
        "rollback",
        "flush",
        "refresh",
        "merge",
        "delete",
        "query",
        "close",
        "connection",
    }
)
SESSION_LIKE_NAMES = frozenset({"session", "db", "db_session", "dbsession", "sess", "s", "async_session"})


def kind_of_type(dotted: Optional[str], facts: Optional["ProjectFacts"]) -> Optional[str]:
    if not dotted:
        return None
    if dotted in TYPE_KINDS:
        return TYPE_KINDS[dotted]
    if facts is not None:
        return facts.alias_kind(dotted)
    return None


def factory_kind(dotted: Optional[str], facts: Optional["ProjectFacts"], local_factories: Dict[str, str], bare: Optional[str]) -> Optional[str]:
    """Kind produced by calling a factory name (SessionLocal, async_session, ...)."""
    if bare is not None and bare in local_factories:
        return local_factories[bare]
    if dotted and facts is not None:
        kind = facts.factory_kind(dotted)
        if kind:
            return kind
    return None


def kind_of_call(
    call: ast.Call,
    resolve,
    facts: Optional["ProjectFacts"],
    local_factories: Dict[str, str],
) -> Optional[str]:
    """Kind of the object produced by ``call`` (constructor, factory call or factory.begin())."""
    func = call.func
    parts = dotted_name(func)
    if parts:
        dotted = resolve(parts)
        if dotted in TYPE_KINDS:
            return TYPE_KINDS[dotted]
        kind = factory_kind(dotted, facts, local_factories, parts[0] if len(parts) == 1 else None)
        if kind:
            return kind
        # Factory.begin() / Factory.begin_nested() used as a context manager
        if len(parts) >= 2 and parts[-1] in {"begin", "begin_nested"}:
            base = parts[:-1]
            base_dotted = resolve(base)
            kind = factory_kind(base_dotted, facts, local_factories, base[0] if len(base) == 1 else None)
            if kind:
                return kind
    return None


def module_level_factories(tree: ast.Module, resolve) -> Dict[str, str]:
    """``{local_name: produced_kind}`` for module-level ``X = sessionmaker(...)`` bindings."""
    found: Dict[str, str] = {}
    for stmt in tree.body:
        targets = []
        value = None
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            targets = [stmt.target]
            value = stmt.value
        if not targets or not isinstance(value, ast.Call):
            continue
        parts = dotted_name(value.func)
        dotted = resolve(parts) if parts else None
        if dotted in FACTORY_KINDS:
            for target in targets:
                found[target.id] = FACTORY_KINDS[dotted]
        elif dotted in {"sqlalchemy.orm.scoped_session", "sqlalchemy.ext.asyncio.async_scoped_session"}:
            for target in targets:
                found[target.id] = FACTORY_KINDS[dotted]
    return found


def function_kinds(
    func: FunctionInfo,
    facts: Optional["ProjectFacts"],
    local_factories: Dict[str, str],
) -> Dict[str, str]:
    """Map local names in ``func`` to a kind, from annotations, assignments and with-items."""
    kinds: Dict[str, str] = {}
    resolve = func.resolve
    for arg in func.params():
        kind = kind_of_type(annotation_to_dotted(arg.annotation, resolve), facts)
        if kind:
            kinds[arg.arg] = kind
    for node in func.scope_nodes():
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            kind = kind_of_type(annotation_to_dotted(node.annotation, resolve), facts)
            if kind:
                kinds[node.target.id] = kind
            elif isinstance(node.value, ast.Call):
                kind = kind_of_call(node.value, resolve, facts, local_factories)
                if kind:
                    kinds[node.target.id] = kind
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            kind = kind_of_call(node.value, resolve, facts, local_factories)
            if kind:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        kinds[target.id] = kind
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call) and isinstance(item.optional_vars, ast.Name):
                    kind = kind_of_call(item.context_expr, resolve, facts, local_factories)
                    if kind:
                        kinds[item.optional_vars.id] = kind
    return kinds


def receiver_kind(call: ast.Call, kinds: Dict[str, str]) -> Optional[str]:
    """Kind of ``x`` in ``x.method(...)`` when ``x`` is a plain name with a known kind."""
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return kinds.get(call.func.value.id)
    return None


def receiver_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id
    return None
