"""Lifecycle of coroutines, locks and HTTP clients (v1.7).

- ``ASYNC_COROUTINE_NOT_AWAITED``: a call to a project *async* function (resolved through the call
  graph, so attribute and instance calls count) whose coroutine is dropped: a bare statement, an
  assignment whose target is never awaited or scheduled, or a ``return`` from an ``async def``.
- ``ASYNC_LOCK_PER_CALL``: a lock constructed inside the function that acquires it.
- ``REL_HTTP_CLIENT_PER_CALL``: an httpx client built inside a function, used there, and neither
  managed with ``with`` nor closed, returned, yielded or stored.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Optional, Set

from ..model import ENV_APPLICATION, Finding, new_finding
from ..resolver import FunctionInfo, dotted_name, iter_scope_nodes
from . import FileContext
from .async_rules import iter_function_calls

CATEGORY = "async"
SCHEDULERS = frozenset({"gather", "create_task", "ensure_future", "wait", "wait_for", "shield", "as_completed", "start_soon", "run", "run_until_complete", "to_thread", "run_sync"})
LOCK_CTORS = frozenset({"asyncio.Lock", "asyncio.locks.Lock", "anyio.Lock", "threading.Lock", "threading.RLock", "asyncio.Semaphore", "asyncio.BoundedSemaphore", "anyio.Semaphore"})
HTTP_CLIENT_CTORS = frozenset({"httpx.AsyncClient", "httpx.Client"})
CLOSERS = frozenset({"aclose", "close"})


def _assigned_name(stmt: ast.AST) -> Optional[str]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


def _awaited_or_scheduled_names(func: FunctionInfo) -> Set[str]:
    """Names that are awaited (``await x``), handed to a scheduler, returned, yielded or stored on self."""
    out: Set[str] = set()
    for node in func.scope_nodes():
        if isinstance(node, ast.Await):
            if isinstance(node.value, ast.Name):
                out.add(node.value.id)  # `await task`; `await client.get()` does not consume the client
        elif isinstance(node, ast.Call):
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else (node.func.id if isinstance(node.func, ast.Name) else None)
            if attr in SCHEDULERS or attr in {"add_task", "append", "add", "extend", "put", "put_nowait"}:
                for a in [*node.args, *[k.value for k in node.keywords]]:
                    out |= {n.id for n in ast.walk(a) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and node.value is not None:
            if isinstance(node.value, ast.Name):
                out.add(node.value.id)  # the object itself is handed to the caller (a factory or a sync wrapper)
            elif not func.is_async:
                out |= {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            # an async function returning a container that holds the coroutine/client still drops it
        elif isinstance(node, ast.Assign) and any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in node.targets):
            out |= {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
    return out


def _coroutines_dropped(ctx: FileContext, func: FunctionInfo, graph, out: List[Finding]) -> None:
    deferred: Set[int] = set()
    for call in iter_function_calls(func):  # calls nested in scheduler arguments are consumed there
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else (call.func.id if isinstance(call.func, ast.Name) else None)
        if attr in SCHEDULERS or attr == "add_task":
            for a in [*call.args, *[k.value for k in call.keywords]]:
                deferred |= {id(n) for n in ast.walk(a) if isinstance(n, ast.Call)}
    kept = _awaited_or_scheduled_names(func)
    for stmt in func.scope_nodes():
        call: Optional[ast.Call] = None
        how = ""
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call, how = stmt.value, "is a bare statement"
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(stmt, "value", None), ast.Call):
            name = _assigned_name(stmt)
            if name is None or name in kept:
                continue
            call, how = stmt.value, f"is assigned to '{name}', which is never awaited or scheduled"
        elif isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call) and func.is_async:
            call, how = stmt.value, "is returned from an async function (the caller receives a coroutine object, not the result)"
        if call is None or id(call) in ctx.awaited or id(call) in deferred:
            continue
        status, key = graph.site_target(ctx.relpath, call.lineno, call.col_offset)
        callee = graph.functions.get(key) if (status == "project" and key) else None
        if callee is None or not callee.is_async or callee.is_route:
            continue
        _kind, evidence = graph.edge_info(ctx.relpath, call.lineno, call.col_offset)
        out.append(
            new_finding(
                "ASYNC_COROUTINE_NOT_AWAITED",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"'{callee.qualname}' ({callee.file}) is an async function, but its call in '{func.qualname}' {how}. "
                    "The coroutine never runs and Python warns 'coroutine was never awaited'. Add 'await', or schedule it "
                    "(gather / create_task with a kept reference / TaskGroup)."
                    + (" The callee was resolved through an inferred edge; confirm the target." if evidence == "inferred" else "")
                ),
                confidence="high" if evidence == "verified" else "medium",
                symbol=callee.qualname,
                function=func.qualname,
                environment=ctx.environment,
                evidence=evidence,
                call_path=[func.qualname, callee.key],
                recommended_test="Run the function under `python -W error::RuntimeWarning` or assert the side effect of the callee; it never happens.",
            )
        )


def _lock_per_call(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    created: Dict[str, int] = {}
    for stmt in func.scope_nodes():
        name = _assigned_name(stmt)
        value = getattr(stmt, "value", None)
        if name and isinstance(value, ast.Call):
            dotted = func.resolve_node(value.func)
            if dotted in LOCK_CTORS:
                created[name] = stmt.lineno
    if not created:
        return
    escaped = _awaited_or_scheduled_names(func)  # returned, yielded or stored locks are factories, not guards
    for node in func.scope_nodes():
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                root = expr.id if isinstance(expr, ast.Name) else None
                if root in created and root not in escaped:
                    out.append(
                        new_finding(
                            "ASYNC_LOCK_PER_CALL",
                            ctx.relpath,
                            created[root],
                            0,
                            (
                                f"'{root}' is created at line {created[root]} inside '{func.qualname}' and acquired at line {node.lineno}. "
                                "Every call gets its own lock, so nothing is mutually excluded between concurrent calls. Create the "
                                "lock once (module level, application state or the object's __init__) and share it."
                            ),
                            confidence="high",
                            symbol=root,
                            function=func.qualname,
                            environment=ctx.environment,
                            recommended_test="Run two concurrent calls that each sleep inside the critical section; with a per-call lock both are inside at the same time.",
                        )
                    )
                    del created[root]
                    break


def _client_per_call(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    created: Dict[str, int] = {}
    for stmt in func.scope_nodes():
        name = _assigned_name(stmt)
        value = getattr(stmt, "value", None)
        if name and isinstance(value, ast.Call):
            dotted = func.resolve_node(value.func)
            if dotted in HTTP_CLIENT_CTORS:
                created[name] = stmt.lineno
    if not created:
        return
    escaped = _awaited_or_scheduled_names(func)
    used: Set[str] = set()
    closed: Set[str] = set()
    for call in iter_function_calls(func):
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id in created:
            if call.func.attr in CLOSERS:
                closed.add(call.func.value.id)
            else:
                used.add(call.func.value.id)
    for name, line in sorted(created.items(), key=lambda kv: kv[1]):
        if name in closed or name in escaped or name not in used:
            continue
        out.append(
            new_finding(
                "REL_HTTP_CLIENT_PER_CALL",
                ctx.relpath,
                line,
                0,
                (
                    f"'{name}' is an HTTP client built at line {line} inside '{func.qualname}', used there and never closed or "
                    "managed with `with`. Each call opens its own connection pool and leaks it until garbage collection; "
                    "connection reuse is lost. Create one client for the application (lifespan, app.state) or use "
                    "`async with httpx.AsyncClient() as client:`."
                ),
                confidence="medium",
                symbol=name,
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Call the function in a loop and watch open sockets (lsof) or ResourceWarnings grow; then share the client and repeat.",
            )
        )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment != ENV_APPLICATION:
        return out  # fixtures build clients and locks freely; test code is covered by the direct rules
    graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
    for func in ctx.functions:
        if graph is not None:
            _coroutines_dropped(ctx, func, graph, out)
        _lock_per_call(ctx, func, out)
        _client_per_call(ctx, func, out)
    return out
