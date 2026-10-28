"""SQLAlchemy sessions and engines: globals, concurrency, await, blocking, lifecycle."""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional, Set

from ..kinds import (
    ASYNC_SESSION_COROUTINES,
    CLIENT_KINDS,
    ENGINE_FACTORIES,
    KIND_ASYNC_SESSION,
    KIND_SESSION,
    SESSION_KINDS,
    SESSION_LIKE_NAMES,
    SYNC_SESSION_IO,
    function_kinds,
    kind_of_call,
    receiver_kind,
    receiver_name,
)
from ..model import ENV_APPLICATION, ENV_MIGRATION, ENV_TEST, Finding, new_finding
from ..resolver import FunctionInfo, dotted_name
from . import FileContext
from .async_rules import iter_function_calls

CATEGORY = "sqlalchemy"

FACTORY_FUNCTION_RE = re.compile(
    r"^(create_app|lifespan|main|startup|on_startup|shutdown|get_engine|make_engine|build_engine|"
    r"engine_factory|init_.*|setup_.*|configure_.*|bootstrap.*|.*_fixture|.*engine.*)$"
)
CACHE_DECORATORS = frozenset(
    {"functools.lru_cache", "functools.cache", "functools.cached_property", "pytest.fixture", "pytest_asyncio.fixture"}
)
GATHER_LIKE = frozenset({"asyncio.gather", "asyncio.wait", "asyncio.as_completed"})
SPAWN_LIKE = frozenset({"asyncio.create_task", "asyncio.ensure_future", "anyio.to_thread.run_sync", "asyncio.to_thread"})
SPAWN_ATTRS = frozenset({"create_task", "start_soon", "submit", "run_in_executor", "map"})
THREAD_CTORS = frozenset({"threading.Thread", "multiprocessing.Process"})
DEFERRED_ATTRS = frozenset({"gather", "create_task", "ensure_future", "wait_for", "shield", "start_soon", "wait", "as_completed"})

SHARED_TEST = (
    "Schedule two of these coroutines concurrently (asyncio.gather) against a real "
    "database with the same session object and observe the concurrent-operation "
    "error or interleaved state; then give each task its own session from the factory."
)


def _names_in(node: ast.AST) -> Set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _deferred_call_ids(func: FunctionInfo) -> Set[int]:
    """Calls passed as arguments to gather/create_task/... are awaited indirectly."""
    ids: Set[int] = set()
    for call in iter_function_calls(func):
        dotted = func.resolve_node(call.func)
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
        if (dotted and dotted.startswith("asyncio.")) or attr in DEFERRED_ATTRS:
            for arg in [*call.args, *[k.value for k in call.keywords]]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Call):
                        ids.add(id(sub))
    return ids


# --------------------------------------------------------------------------- #
# module level
# --------------------------------------------------------------------------- #


def _module_global_sessions(ctx: FileContext, out: List[Finding]) -> None:
    for stmt in ctx.tree.body:  # direct module statements only (not under if __name__ ...)
        targets: List[ast.Name] = []
        value: Optional[ast.AST] = None
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets = [stmt.target]
            value = stmt.value
        if not targets or not isinstance(value, ast.Call):
            continue
        kind = kind_of_call(value, ctx.resolve, ctx.facts, ctx.local_factories)
        if kind not in SESSION_KINDS:
            continue
        parts = dotted_name(value.func) or []
        symbol = ctx.resolve(parts) or ".".join(parts) or "session"
        out.append(
            new_finding(
                "SA_SESSION_MODULE_GLOBAL",
                ctx.relpath,
                stmt.lineno,
                stmt.col_offset,
                (
                    f"'{targets[0].id}' is a {kind} instance created at import time via "
                    f"'{symbol}' and shared by every request and task that imports this "
                    "module. Keep the factory global and create one session per request "
                    "or unit of work (dependency with yield, or a context manager)."
                ),
                confidence="high",
                symbol=symbol,
                environment=ctx.environment,
            )
        )


# --------------------------------------------------------------------------- #
# function level
# --------------------------------------------------------------------------- #


def _check_await_and_blocking(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], deferred: Set[int], out: List[Finding]) -> None:
    for call in iter_function_calls(func):
        if not isinstance(call.func, ast.Attribute):
            continue
        attr = call.func.attr
        kind = receiver_kind(call, kinds)
        name = receiver_name(call)
        awaited = id(call) in ctx.awaited or id(call) in deferred
        if kind == KIND_ASYNC_SESSION and attr in ASYNC_SESSION_COROUTINES and not awaited:
            if id(call) in ctx.returned and not func.is_async:
                continue  # a sync helper handing the awaitable to its caller
            out.append(
                new_finding(
                    "SA_ASYNC_SESSION_NOT_AWAITED",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}.{attr}()' is a coroutine method of an AsyncSession but is not "
                        f"awaited in '{func.qualname}', so it never executes (and Python warns "
                        "'coroutine was never awaited'). Add 'await'."
                    ),
                    confidence="high",
                    symbol=f"AsyncSession.{attr}",
                    function=func.qualname,
                    environment=ctx.environment,
                )
            )
            continue
        if not func.is_async or awaited:
            continue
        if kind == KIND_SESSION and attr in SYNC_SESSION_IO:
            out.append(
                new_finding(
                    "SA_SYNC_SESSION_IN_ASYNC_DEF",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}.{attr}()' performs synchronous database I/O on a Session inside "
                        f"async function '{func.qualname}', blocking the event loop. Use an "
                        "AsyncSession, run the work in a thread (anyio.to_thread.run_sync), or "
                        "declare the route with plain 'def'."
                    ),
                    confidence="high",
                    symbol=f"Session.{attr}",
                    function=func.qualname,
                    environment=ctx.environment,
                    recommended_test="Measure event-loop lag or concurrent latency while this handler runs a slow query.",
                )
            )
        elif kind is None and name and attr == "query" and name in SESSION_LIKE_NAMES:
            out.append(
                new_finding(
                    "SA_SYNC_SESSION_IN_ASYNC_DEF",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}.query()' uses the legacy Query API, which exists only on the "
                        f"synchronous Session, inside async function '{func.qualname}'. If "
                        f"'{name}' is a sync Session this blocks the event loop."
                    ),
                    confidence="medium",
                    symbol="Session.query",
                    function=func.qualname,
                    environment=ctx.environment,
                )
            )
        elif kind is None and name in SESSION_LIKE_NAMES and attr in {"commit", "execute", "flush", "refresh", "rollback", "scalar", "scalars"}:
            out.append(
                new_finding(
                    "SA_SYNC_SESSION_IN_ASYNC_DEF",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}.{attr}()' is called without await inside async function "
                        f"'{func.qualname}' and the type of '{name}' is not visible here. Either "
                        "it is a synchronous Session blocking the event loop, or an AsyncSession "
                        "call that is missing 'await'. Check the annotation or the factory."
                    ),
                    confidence="low",
                    symbol=f"?.{attr}",
                    function=func.qualname,
                    environment=ctx.environment,
                )
            )


def _check_shared_concurrently(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding]) -> None:
    session_names = {n for n, k in kinds.items() if k in SESSION_KINDS}
    for call in iter_function_calls(func):
        dotted = func.resolve_node(call.func)
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
        is_gather = dotted in GATHER_LIKE
        is_spawn = dotted in SPAWN_LIKE or dotted in THREAD_CTORS or attr in SPAWN_ATTRS
        if not (is_gather or is_spawn):
            continue
        # `await asyncio.to_thread(session.commit)` / `await loop.run_in_executor(...)` offload one
        # call and wait for it: sequential use from another thread, not concurrent sharing.
        offload = dotted in {"asyncio.to_thread", "anyio.to_thread.run_sync"} or attr == "run_in_executor"
        if offload and id(call) in ctx.awaited:
            continue
        args = [*call.args, *[k.value for k in call.keywords]]
        per_arg = [_names_in(a) for a in args]
        starred = any(isinstance(a, ast.Starred) for a in call.args)
        candidates = session_names | {n for n in SESSION_LIKE_NAMES if any(n in s for s in per_arg)}
        for sname in sorted(candidates):
            count = sum(1 for s in per_arg if sname in s)
            if is_gather and not (count >= 2 or (starred and count >= 1)):
                continue
            if is_spawn and count < 1:
                continue
            known = sname in session_names
            kind = kinds.get(sname, "session")
            how = "asyncio.gather/wait" if is_gather else (dotted or attr or "a concurrent scheduler")
            out.append(
                new_finding(
                    "SA_SESSION_SHARED_CONCURRENTLY",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{sname}' ({kind}{'' if known else ', type inferred from its name'}) is passed to "
                        f"{'several coroutines scheduled together by' if is_gather else 'work scheduled concurrently by'} "
                        f"{how} in '{func.qualname}'. A session is not safe for concurrent use; "
                        "create one session per task (async_sessionmaker() inside each coroutine) "
                        "or run the steps sequentially."
                    ),
                    confidence=("high" if (known and is_gather) else "medium" if known else "low"),
                    symbol=how,
                    function=func.qualname,
                    environment=ctx.environment,
                    recommended_test=SHARED_TEST,
                )
            )
            break  # one finding per scheduler call


def _check_engine_per_call(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    for call in iter_function_calls(func):
        dotted = func.resolve_node(call.func)
        if dotted not in ENGINE_FACTORIES:
            continue
        if func.route is None:
            if FACTORY_FUNCTION_RE.match(func.name) or func.name.startswith("_"):
                continue
            if any(d in CACHE_DECORATORS or (d or "").endswith(("fixture", "cache", "cached")) for d in func.decorators):
                continue
            if ctx.environment == ENV_TEST:
                continue
            confidence = "medium"
            where = f"function '{func.qualname}'"
        else:
            confidence = "high"
            where = f"route handler '{func.qualname}' ({func.route.method} {func.route.path or ''})".rstrip()
        out.append(
            new_finding(
                "SA_ENGINE_CREATED_PER_CALL",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"'{dotted}' is called inside {where}, so a new engine and connection pool "
                    "are built on every call and never disposed. Create the engine once in the "
                    "lifespan (or a cached factory) and inject sessions from it."
                ),
                confidence=confidence,
                symbol=dotted,
                function=func.qualname,
                environment=ctx.environment,
            )
        )


def _check_create_all(ctx: FileContext, func: Optional[FunctionInfo], calls, out: List[Finding]) -> None:
    if ctx.environment != ENV_APPLICATION:
        return
    for call in calls:
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "create_all"):
            continue
        recv = call.func.value
        is_metadata = (isinstance(recv, ast.Attribute) and recv.attr == "metadata") or (
            isinstance(recv, ast.Name) and recv.id in {"metadata", "Base"}
        )
        if not is_metadata:
            continue
        where = f"in '{func.qualname}'" if func else "at module level"
        out.append(
            new_finding(
                "SA_CREATE_ALL_AT_RUNTIME",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"metadata.create_all() runs {where}. It creates missing tables but never "
                    "alters existing ones, so production schemas drift from the models. Fine for "
                    "a prototype or a throwaway database; use Alembic migrations otherwise."
                ),
                confidence="medium",
                symbol="metadata.create_all",
                function=func.qualname if func else None,
                environment=ctx.environment,
            )
        )


def _check_session_not_closed(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    created: Dict[str, ast.Assign] = {}
    for node in func.scope_nodes():
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Await):
                value = value.value
            if not isinstance(value, ast.Call):
                continue
            # only factory/constructor calls, not Factory.begin() (that is used with "with")
            if isinstance(value.func, ast.Attribute) and value.func.attr in {"begin", "begin_nested"}:
                continue
            kind = kind_of_call(value, func.resolve, ctx.facts, ctx.local_factories)
            if kind in SESSION_KINDS:
                created[node.targets[0].id] = node
    if not created:
        return
    closed: Set[str] = set()
    handed_over: Set[str] = set()
    yielded: Set[str] = set()
    for node in func.scope_nodes():
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.attr == "close":
                closed.add(node.func.value.id)
            for arg in [*node.args, *[k.value for k in node.keywords]]:
                if isinstance(arg, ast.Name):
                    handed_over.add(arg.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Name):
                    closed.add(item.context_expr.id)
        elif isinstance(node, ast.Return) and node.value is not None:
            handed_over |= _names_in(node.value)
        elif isinstance(node, ast.Yield) and node.value is not None:
            yielded |= _names_in(node.value)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if any(isinstance(t, ast.Attribute) for t in node.targets):
                handed_over.add(node.value.id)  # self.session = session
    for name, node in sorted(created.items()):
        if name in closed:
            continue
        if name in yielded:
            confidence, detail = "high", "it is yielded as a dependency but never closed; close it in a 'finally' block or use a context manager"
        elif name in handed_over:
            continue  # ownership moved to a caller or another object
        else:
            confidence, detail = "medium", "no close(), context manager or hand-over to a caller is visible in this function"
        out.append(
            new_finding(
                "SA_SESSION_NOT_CLOSED",
                ctx.relpath,
                node.lineno,
                node.col_offset,
                f"'{name}' is created from a session factory in '{func.qualname}' and {detail}. The pooled connection is only released when the session is garbage-collected.",
                confidence=confidence,
                symbol="session.close",
                function=func.qualname,
                environment=ctx.environment,
            )
        )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment == ENV_MIGRATION or ctx.alembic_imported:
        return out  # Alembic env.py and revisions manage engines and sessions by design
    if not (ctx.sqlalchemy_imported or ctx.local_factories or (ctx.facts and ctx.facts.uses_sqlalchemy)):
        return out
    _module_global_sessions(ctx, out)
    module_calls = [n for n in ast.walk(ctx.tree) if isinstance(n, ast.Call)]
    in_functions = {id(c) for f in ctx.functions for c in iter_function_calls(f)}
    _check_create_all(ctx, None, [c for c in module_calls if id(c) not in in_functions], out)
    for func in ctx.functions:
        kinds = function_kinds(func, ctx.facts, ctx.local_factories)
        deferred = _deferred_call_ids(func)
        _check_await_and_blocking(ctx, func, kinds, deferred, out)
        _check_shared_concurrently(ctx, func, kinds, out)
        _check_engine_per_call(ctx, func, out)
        _check_create_all(ctx, func, iter_function_calls(func), out)
        _check_session_not_closed(ctx, func, out)
    return out
