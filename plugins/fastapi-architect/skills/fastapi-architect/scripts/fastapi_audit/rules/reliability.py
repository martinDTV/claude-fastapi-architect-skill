"""Reliability signals: retried writes without idempotency, request-scoped resources and
context handed to background work. All three lean on the function summaries."""

from __future__ import annotations

import ast
from typing import List, Optional, Set, Tuple

from ..kinds import SESSION_KINDS, function_kinds_ex
from ..model import ENV_APPLICATION, Finding, new_finding
from ..resolver import FunctionInfo, dotted_name
from . import FileContext
from .async_rules import iter_function_calls

CATEGORY = "reliability"
SPAWNERS = frozenset({"asyncio.create_task", "asyncio.ensure_future"})
SPAWN_ATTRS = frozenset({"create_task", "start_soon", "add_task"})
WRITE_DEPTH = 2


def _reachable_writes(graph, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[List[str], str]]:
    record = graph.functions.get(key)
    if record is None:
        return None
    if record.writes:
        return [key], record.writes[0][1]
    if depth >= WRITE_DEPTH:
        return None
    visited = (visited or set()) | {key}
    for _site, callee in graph.edges.get(key, []):
        if callee in visited:
            continue
        sub = _reachable_writes(graph, callee, depth + 1, visited)
        if sub:
            return [key, *sub[0]], sub[1]
    return None


def _recreates_context(graph, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> bool:
    record = graph.functions.get(key)
    if record is None:
        return False
    if record.creates_session or record.sets_tenant_context:
        return True
    if depth >= WRITE_DEPTH:
        return False
    visited = (visited or set()) | {key}
    return any(_recreates_context(graph, callee, depth + 1, visited) for _s, callee in graph.edges.get(key, []) if callee not in visited)


def _idempotency_signal(graph, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> bool:
    record = graph.functions.get(key)
    if record is None:
        return False
    if record.idempotency_signal:
        return True
    if depth >= WRITE_DEPTH:
        return False
    visited = (visited or set()) | {key}
    return any(_idempotency_signal(graph, callee, depth + 1, visited) for _s, callee in graph.edges.get(key, []) if callee not in visited)


def _resolve_callable(ctx: FileContext, func: FunctionInfo, node: ast.AST, graph) -> Optional[str]:
    """Function key for a callable reference passed as an argument (add_task(fn, ...))."""
    parts = dotted_name(node)
    if not parts:
        return None
    dotted = func.resolve(parts)
    key = graph.canonical_function(dotted) if dotted else None
    if key:
        return key
    module = ctx.facts.file_module.get(ctx.relpath, "") if ctx.facts else ""
    local = f"{module}.{'.'.join(parts)}" if module else ".".join(parts)
    key = graph.canonical_function(local)
    if key:
        return key
    if parts[0] in {"self", "cls"} and func.class_name and len(parts) == 2:
        return graph.canonical_function(f"{module}.{func.class_name}.{parts[1]}")
    return None


# --------------------------------------------------------------------------- #


def _retry(ctx: FileContext, func: FunctionInfo, record, graph, out: List[Finding]) -> None:
    if record is None or not (record.retry_decorated or record.retry_loop):
        return
    path = _reachable_writes(graph, record.key)
    if not path:
        return
    if _idempotency_signal(graph, record.key):
        return
    chain, kind = path
    direct = len(chain) == 1
    how = "a retry decorator" if record.retry_decorated else "a hand-written retry loop"
    out.append(
        new_finding(
            "RETRY_NON_IDEMPOTENT_WRITE",
            ctx.relpath,
            func.lineno,
            getattr(func.node, "col_offset", 0),
            (
                f"'{func.qualname}' runs under {how} and performs a {'database' if kind == 'db' else 'HTTP'} write "
                + ("directly" if direct else f"through {' -> '.join(chain[1:])}")
                + ". No idempotency signal is visible (idempotency key, request/dedup id, ON CONFLICT). "
                "If the first attempt was applied but its response was lost, the retry applies it again. "
                "If the operation is idempotent by design, document it and suppress this finding."
            ),
            confidence="medium" if direct else "low",
            symbol="retry",
            function=func.qualname,
            environment=ctx.environment,
            evidence="verified" if direct else "inferred",
            call_path=None if direct else [*chain],
            recommended_test=(
                "Make the dependency apply the write and then fail the response (timeout or connection reset); "
                "run the retried operation and assert exactly one effect."
            ),
        )
    )


def _background(ctx: FileContext, func: FunctionInfo, record, kinds, evidence, graph, out: List[Finding]) -> None:
    for call in iter_function_calls(func):
        dotted = func.resolve_node(call.func)
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
        is_add_task = attr == "add_task" and (
            (record is not None and record.background_task_param and isinstance(call.func.value, ast.Name) and call.func.value.id == record.background_task_param)
            or (isinstance(call.func.value, ast.Name) and call.func.value.id in {"background_tasks", "tasks", "bg"})
        )
        is_spawn = dotted in SPAWNERS or (attr in {"create_task", "start_soon"} and not is_add_task)
        if not (is_add_task or is_spawn):
            continue
        # the task callable and the names handed to it
        task_node: Optional[ast.AST] = None
        handed: Set[str] = set()
        if is_add_task or attr == "start_soon":
            if call.args:
                task_node = call.args[0]
                for arg in call.args[1:]:
                    if isinstance(arg, ast.Name):
                        handed.add(arg.id)
                for kw in call.keywords:
                    if isinstance(kw.value, ast.Name):
                        handed.add(kw.value.id)
        elif call.args and isinstance(call.args[0], ast.Call):
            inner = call.args[0]
            task_node = inner.func
            for sub in ast.walk(inner):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    handed.add(sub.id)
        # 1. request-scoped session handed to the task
        request_scoped = []
        for name in sorted(handed):
            kind = kinds.get(name)
            if kind not in SESSION_KINDS:
                continue
            from_dependency = record is not None and name in record.dependency_kinds
            if func.route is not None or from_dependency:
                request_scoped.append((name, kind, evidence.get(name, "verified")))
        if request_scoped:
            name, kind, ev = request_scoped[0]
            out.append(
                new_finding(
                    "BG_REQUEST_SCOPED_RESOURCE",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}' ({kind}) is request-scoped and is handed to background work by "
                        f"'{attr or dotted}' in '{func.qualname}'. The dependency that provided it closes the "
                        "session when the response is sent, so the task runs on a closed session or keeps a "
                        "pooled connection alive. Create a session inside the task (async_sessionmaker) and pass ids, not objects."
                    ),
                    confidence="high" if ev == "verified" else "medium",
                    symbol=attr or dotted,
                    function=func.qualname,
                    environment=ctx.environment,
                    evidence=ev,
                    recommended_test="Run the endpoint, let the task outlive the response, and assert the task's database work succeeds and the pool returns to baseline.",
                )
            )
        # 2. authorization / tenant context classification for the task. Only when the
        # spawning function holds request context (a route, a dependency-injected or
        # principal parameter): a worker spawning its own subtasks drops nothing.
        holds_request_context = func.route is not None or (record is not None and (record.depends or record.principal_params))
        if task_node is None or graph is None or not holds_request_context:
            continue
        target_key = _resolve_callable(ctx, func, task_node, graph)
        if not target_key:
            continue
        target = graph.functions.get(target_key)
        writes = _reachable_writes(graph, target_key)
        if target is None or not writes:
            continue
        captured = bool(target.principal_params) or any(kinds.get(n) in SESSION_KINDS for n in handed) or any(
            any(tok in n.lower() for tok in ("user", "principal", "tenant", "actor", "identity", "member", "org")) for n in handed
        )
        recreated = _recreates_context(graph, target_key)
        if captured or recreated:
            continue
        out.append(
            new_finding(
                "BG_TASK_CONTEXT_UNVERIFIED",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"Background task '{target.qualname}' ({target.file}) writes to "
                    f"{'the database' if writes[1] == 'db' else 'an external system'} and receives no tenant or principal "
                    "context from this call, nor does it visibly recreate one (own session, SET LOCAL/set_config). "
                    "Context: not captured, not recreated, not verifiable statically. Confirm how authorization and "
                    "tenant scope apply inside the task."
                ),
                confidence="low",
                symbol=target.qualname,
                function=func.qualname,
                environment=ctx.environment,
                evidence="unknown",
                call_path=[func.qualname, *writes[0]],
                recommended_test="Trigger the task as a member of tenant A with data of tenant B reachable; assert the task only touches A's rows.",
            )
        )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment != ENV_APPLICATION:
        return out  # fixtures and migrations spawn tasks and retry freely; the signals target application code
    facts = ctx.facts
    graph = getattr(facts, "callgraph", None) if facts is not None else None
    if graph is None:
        return out
    for func in ctx.functions:
        record = graph.record_for(ctx.relpath, func.qualname)
        kinds, evidence = function_kinds_ex(func, facts, ctx.local_factories, ctx.relpath)
        _retry(ctx, func, record, graph, out)
        _background(ctx, func, record, kinds, evidence, graph, out)
    return out
