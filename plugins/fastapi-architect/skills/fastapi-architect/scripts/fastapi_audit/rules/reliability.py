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
        for func in ctx.functions:
            kinds, _evidence = function_kinds_ex(func, facts, ctx.local_factories, ctx.relpath)
            _run_v16(ctx, func, None, kinds, out)
        return out
    for func in ctx.functions:
        record = graph.record_for(ctx.relpath, func.qualname)
        kinds, evidence = function_kinds_ex(func, facts, ctx.local_factories, ctx.relpath)
        _retry(ctx, func, record, graph, out)
        _background(ctx, func, record, kinds, evidence, graph, out)
        _run_v16(ctx, func, record, kinds, out)
    return out


# --------------------------------------------------------------------------- #
# v1.6: dropped task references, unbounded retries, HTTP calls without timeout, dual writes
# --------------------------------------------------------------------------- #

import re as _re

from ..kinds import CLIENT_KINDS as _CLIENT_KINDS, KIND_CLIENT as _KIND_CLIENT, receiver_kind as _receiver_kind, receiver_name as _receiver_name
from ..resolver import annotation_to_dotted as _annotation_to_dotted, iter_scope_nodes as _iter_scope_nodes
from .transactions import _reachable_network

TASK_SPAWNERS = frozenset({"asyncio.create_task", "asyncio.ensure_future"})
REQUESTS_CALLS = frozenset({f"requests.{v}" for v in ("get", "post", "put", "patch", "delete", "head", "options", "request")})
HTTPX_CLIENTS = frozenset({"httpx.Client", "httpx.AsyncClient"})
URLOPEN = frozenset({"urllib.request.urlopen"})
RETRY_BOUNDS = {  # decorator -> keywords that bound it (absent => retries forever by the library's default)
    "tenacity.retry": ("stop",),
    "backoff.on_exception": ("max_tries", "max_time"),
    "backoff.on_predicate": ("max_tries", "max_time"),
    "retrying.retry": ("stop_max_attempt_number", "stop_max_delay"),
    "retry.retry": ("tries",),
    "retry.api.retry": ("tries",),
}
STRONG_PUBLISH = frozenset({"publish", "produce", "send_and_wait", "apply_async", "send_task", "enqueue", "enqueue_job", "xadd", "send_message", "basic_publish", "publish_event", "emit_event"})
WEAK_PUBLISH = frozenset({"send", "put", "delay", "emit", "push"})
PRODUCER_RE = _re.compile(r"producer|broker|kafka|publisher|bus|topic|queue|sqs|sns|rabbit|channel|exchange|stream|pubsub|nats|celery|dramatiq|arq", _re.I)
OUTBOX_RE = _re.compile(r"outbox|inbox", _re.I)
COUNTER_TOKENS = ("attempt", "retr", "tries")


def _task_reference(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    for node in func.scope_nodes():
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        dotted = func.resolve_node(call.func)
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
        loop_task = attr == "create_task" and _receiver_name(call) in {"loop", "event_loop"}
        if dotted not in TASK_SPAWNERS and not loop_task:
            continue
        symbol = dotted or f"{_receiver_name(call)}.create_task"
        out.append(
            new_finding(
                "ASYNC_TASK_REFERENCE_DROPPED",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"'{symbol}(...)' in '{func.qualname}' is a bare statement: nothing keeps a reference to the task. "
                    "The event loop holds tasks weakly, so the task can be garbage-collected before it finishes and its "
                    "exception is never observed. Store it (a set with add_done_callback(discard)), await it, or run it "
                    "in an asyncio.TaskGroup."
                ),
                confidence="high",
                symbol=symbol,
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Spawn the task, drop every reference, force gc.collect() while it awaits, and assert the side effect still happens.",
            )
        )


def _retry_unbounded(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    for dec in getattr(func.node, "decorator_list", []):
        call = dec if isinstance(dec, ast.Call) else None
        target = dec.func if call is not None else dec
        parts = dotted_name(target)
        dotted = func.resolve(parts) if parts else None
        if dotted not in RETRY_BOUNDS:
            continue
        bounds = RETRY_BOUNDS[dotted]
        if call is not None and any(kw.arg in bounds for kw in call.keywords):
            continue
        out.append(
            new_finding(
                "RETRY_UNBOUNDED",
                ctx.relpath,
                dec.lineno,
                dec.col_offset,
                (
                    f"'@{dotted}' on '{func.qualname}' has no {' / '.join(bounds)} argument, so by the library's default it "
                    "retries forever. A dependency that stays down turns every call into a hang that holds the request, "
                    "the worker and any open transaction. Bound attempts and total time; keep backoff with jitter."
                ),
                confidence="high",
                symbol=dotted,
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Point the operation at a dependency that always fails and assert it gives up within the documented budget.",
            )
        )
    for node in func.scope_nodes():
        if not isinstance(node, ast.While):
            continue
        test = node.test
        forever = isinstance(test, ast.Constant) and bool(test.value) is True
        if not forever:
            continue
        tries = [n for n in _iter_scope_nodes(node.body) if isinstance(n, ast.Try)]
        if not tries:
            continue
        exits = any(isinstance(n, (ast.Break, ast.Return)) for t in tries for n in _iter_scope_nodes(t.body))
        if not exits:
            continue  # a worker loop that never leaves is not a retry
        counter = any(
            (isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and any(t in n.target.id.lower() for t in COUNTER_TOKENS))
            or (isinstance(n, ast.Compare) and any(isinstance(c, ast.Name) and any(t in c.id.lower() for t in COUNTER_TOKENS) for c in [n.left, *n.comparators]))
            for n in ast.walk(node)
        )
        if counter:
            continue
        out.append(
            new_finding(
                "RETRY_UNBOUNDED",
                ctx.relpath,
                node.lineno,
                node.col_offset,
                (
                    f"'while True' retry loop at line {node.lineno} in '{func.qualname}': the body exits on success but "
                    "goes around again on every exception with no attempt counter or deadline. A persistent failure "
                    "loops forever. Add a bounded attempt count or a total time budget and give up explicitly."
                ),
                confidence="medium",
                symbol="while True",
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Make the operation fail permanently and assert the loop terminates with an error within the budget.",
            )
        )


def _receiver_type(ctx: FileContext, func: FunctionInfo, call: ast.Call) -> Optional[str]:
    """Declared type of ``x`` in ``x.method()``: a self attribute typed in __init__, an annotated
    parameter, or a local built by a constructor call. Canonical through re-exports; None when unknown."""
    name = _receiver_name(call)
    if not name or ctx.facts is None:
        return None
    graph = getattr(ctx.facts, "callgraph", None)
    if name.startswith("self.") and func.class_name and graph is not None:
        cls = graph.class_record_for(ctx.relpath, func.class_name)
        info = cls.attrs.get(name[5:]) if cls is not None else None
        return ctx.facts.canonical(info.type) if info is not None and info.type else None
    for arg in func.params():
        if arg.arg == name:
            return ctx.facts.canonical(_annotation_to_dotted(arg.annotation, func.resolve))
    for node in func.scope_nodes():
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets) and isinstance(node.value, ast.Call):
            parts = dotted_name(node.value.func)
            return ctx.facts.canonical(func.resolve(parts)) if parts else None
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == name and isinstance(item.context_expr, ast.Call):
                    parts = dotted_name(item.context_expr.func)
                    return ctx.facts.canonical(func.resolve(parts)) if parts else None
    return None


def _http_timeout(ctx: FileContext, func: FunctionInfo, kinds, out: List[Finding]) -> None:
    for call in iter_function_calls(func):
        dotted = func.resolve_node(call.func)
        keywords = {kw.arg for kw in call.keywords if kw.arg}
        attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
        what: Optional[str] = None
        confidence = "high"
        if dotted in REQUESTS_CALLS and "timeout" not in keywords:
            what = f"'{dotted}()' without timeout= (requests never times out by default)"
        elif dotted in URLOPEN and "timeout" not in keywords and len(call.args) < 2:
            what, confidence = f"'{dotted}()' without timeout (blocks until the socket default, usually forever)", "medium"
        elif dotted in HTTPX_CLIENTS:
            for kw in call.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant) and kw.value.value is None:
                    what = f"'{dotted}(timeout=None)' disables httpx's default 5 s timeout for every request on this client"
        elif attr in {"get", "post", "put", "patch", "delete", "head", "options", "request"} and "timeout" not in keywords:
            if _receiver_kind(call, kinds) == _KIND_CLIENT:
                rtype = _receiver_type(ctx, func, call) or ""
                if rtype.startswith("requests."):  # httpx clients carry a default timeout; requests.Session does not
                    what, confidence = f"'{_receiver_name(call)}.{attr}()' on a requests.Session without timeout=", "medium"
        if what is None:
            continue
        out.append(
            new_finding(
                "REL_HTTP_CALL_WITHOUT_TIMEOUT",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"{what} in '{func.qualname}'. A stalled dependency then holds this worker, its pooled database "
                    "connection and any open transaction indefinitely. Pass an explicit timeout (connect and read) and "
                    "decide what the caller sees when it fires."
                ),
                confidence=confidence,
                symbol=dotted or f"{_receiver_name(call)}.{attr}",
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Point the call at a server that accepts the connection and never answers; assert the call fails within the configured timeout.",
            )
        )


def _dual_write(ctx: FileContext, func: FunctionInfo, record, kinds, out: List[Finding]) -> None:
    commits = []
    publishes = []
    for call in iter_function_calls(func):
        if not isinstance(call.func, ast.Attribute):
            continue
        attr = call.func.attr
        name = _receiver_name(call) or ""
        kind = _receiver_kind(call, kinds)
        if attr == "commit" and (kind in SESSION_KINDS or name in {"session", "db", "db_session", "self._session", "self.session", "self.db"}):
            commits.append(call)
        elif attr in STRONG_PUBLISH or (attr in WEAK_PUBLISH and PRODUCER_RE.search(name) and kind not in SESSION_KINDS and kind not in _CLIENT_KINDS):
            publishes.append((call, attr in STRONG_PUBLISH))
    if not commits or not publishes:
        return
    names = {n.id for n in ast.walk(func.node) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(func.node) if isinstance(n, ast.Attribute)}
    texts = [n.value for n in ast.walk(func.node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    if any(OUTBOX_RE.search(n) for n in names) or any(OUTBOX_RE.search(t) for t in texts):
        return
    graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
    external_publishes = []
    for call, strong in publishes:
        if graph is not None:
            status, key = graph.site_target(ctx.relpath, call.lineno, call.col_offset)
            if status == "project" and key:
                # a project-owned queue: only a dual write when it leaves the database (network) itself
                if _reachable_network(graph, key) is None:
                    continue
            elif status == "builtin":
                continue  # a list/deque/dict: not a broker (module-level objects may well be one; kept)
        external_publishes.append((call, strong))
    publishes = external_publishes
    if not publishes:
        return
    call, strong = publishes[0]
    receiver = _receiver_name(call) or "?"
    out.append(
        new_finding(
            "REL_DUAL_WRITE_WITHOUT_OUTBOX",
            ctx.relpath,
            call.lineno,
            call.col_offset,
            (
                f"'{func.qualname}' commits a database transaction (line {commits[0].lineno}) and publishes through "
                f"'{receiver}.{call.func.attr}()' (line {call.lineno}) with no outbox or inbox table in sight. The two "
                "writes cannot be atomic: a crash or a broker error between them loses the event, and publishing "
                "before the commit announces a change that may roll back. Insert an outbox row in the same transaction "
                "and relay it from a worker; make the consumer idempotent."
            ),
            confidence="medium" if strong else "low",
            symbol=f"{receiver}.{call.func.attr}",
            function=func.qualname,
            environment=ctx.environment,
            recommended_test="Kill the process between commit and publish (fault injection); assert the event is still delivered after restart, exactly once.",
        )
    )


def _run_v16(ctx: FileContext, func: FunctionInfo, record, kinds, out: List[Finding]) -> None:
    _task_reference(ctx, func, out)
    _retry_unbounded(ctx, func, out)
    _http_timeout(ctx, func, kinds, out)
    _dual_write(ctx, func, record, kinds, out)
