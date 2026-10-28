"""v1.3 rules: blocking calls in async functions, nested asyncio.run, route annotations."""

from __future__ import annotations

import ast
from typing import Dict, Iterator, List

from ..model import Finding, new_finding
from ..resolver import FunctionInfo, dotted_name
from . import FileContext

CATEGORY = "async"  # ROUTE_NO_RETURN_ANNOTATION reports under "routes"

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")

# Fully qualified callable -> confidence that calling it blocks the event loop.
BLOCKING_CALLS: Dict[str, str] = {
    "time.sleep": "high",
    "urllib.request.urlopen": "high",
    "urllib.request.urlretrieve": "high",
    "subprocess.run": "high",
    "subprocess.call": "high",
    "subprocess.check_call": "high",
    "subprocess.check_output": "high",
    "subprocess.getoutput": "high",
    "subprocess.getstatusoutput": "high",
    "os.system": "high",
    "os.popen": "medium",
    "socket.create_connection": "medium",
    "smtplib.SMTP": "medium",
    "psycopg2.connect": "medium",
    "pymysql.connect": "medium",
    "sqlite3.connect": "medium",
}
for _verb in HTTP_VERBS:
    BLOCKING_CALLS["requests." + _verb] = "high"
    BLOCKING_CALLS["httpx." + _verb] = "high"  # module-level helpers are sync
BLOCKING_CALLS["httpx.stream"] = "high"

EVENT_LOOP_FACTORIES = frozenset(
    {"asyncio.get_event_loop", "asyncio.new_event_loop", "asyncio.get_running_loop"}
)


def _collect_function_calls(func: FunctionInfo) -> List[ast.Call]:
    calls: List[ast.Call] = []
    for node in func.scope_nodes():
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            extras: List[ast.AST] = list(node.decorator_list)
            extras.extend(d for d in node.args.defaults if d is not None)
            extras.extend(d for d in node.args.kw_defaults if d is not None)
            for extra in extras:
                for sub in ast.walk(extra):
                    if isinstance(sub, ast.Call):
                        calls.append(sub)
    return calls


def iter_function_calls(func: FunctionInfo) -> Iterator[ast.Call]:
    """Call nodes executed in ``func``'s own scope (cached on the FunctionInfo).

    Nested function bodies are excluded, but their decorators and default
    values run in this scope and are included.
    """
    if func.calls_cache is None:
        func.calls_cache = _collect_function_calls(func)
    return iter(func.calls_cache)


def _check_blocking(ctx: FileContext, func: FunctionInfo, call: ast.Call, out: List[Finding]) -> None:
    parts = dotted_name(call.func)
    if not parts:
        return
    resolved = func.resolve(parts)
    if resolved is None:
        return
    confidence = BLOCKING_CALLS.get(resolved)
    if confidence is None:
        return
    out.append(
        new_finding(
            "ASYNC_BLOCKING_CALL",
            ctx.relpath,
            call.lineno,
            call.col_offset,
            (
                f"'{resolved}' is called directly inside async function "
                f"'{func.qualname}'. It blocks the event loop while it runs. "
                "If intentional, offload it (anyio.to_thread.run_sync or "
                "asyncio.to_thread), use an async client, or declare the function "
                "with plain 'def'. Verify before changing."
            ),
            confidence=confidence,
            symbol=resolved,
            function=func.qualname,
            environment=ctx.environment,
            recommended_test=(
                "Fire this endpoint concurrently with a trivial endpoint on one worker "
                "and measure the trivial endpoint's latency; or measure event-loop lag "
                "while the call runs."
            ),
        )
    )


def _check_nested_run(ctx: FileContext, func: FunctionInfo, call: ast.Call, out: List[Finding]) -> None:
    parts = dotted_name(call.func)
    if parts and func.resolve(parts) == "asyncio.run":
        out.append(
            new_finding(
                "ASYNCIO_RUN_NESTED",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"'asyncio.run' is called inside async function '{func.qualname}'. "
                    "asyncio.run cannot be called when an event loop is already running; "
                    "await the coroutine instead."
                ),
                confidence="high",
                symbol="asyncio.run",
                function=func.qualname,
                environment=ctx.environment,
                recommended_test="Call the code path from a running loop in a test and expect RuntimeError before the fix.",
            )
        )
        return
    if isinstance(call.func, ast.Attribute) and call.func.attr == "run_until_complete":
        confidence = "medium"
        receiver = call.func.value
        if isinstance(receiver, ast.Call):
            inner = dotted_name(receiver.func)
            if inner and func.resolve(inner) in EVENT_LOOP_FACTORIES:
                confidence = "high"
        out.append(
            new_finding(
                "ASYNCIO_RUN_NESTED",
                ctx.relpath,
                call.lineno,
                call.col_offset,
                (
                    f"'run_until_complete' is called inside async function '{func.qualname}'. "
                    "Driving an event loop from a coroutine raises RuntimeError on a running "
                    "loop; await the coroutine instead. Confirm the receiver is an event loop."
                ),
                confidence=confidence,
                symbol="run_until_complete",
                function=func.qualname,
                environment=ctx.environment,
            )
        )


def _check_route_annotation(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    route = func.route
    if route is None or getattr(func.node, "returns", None) is not None:
        return
    if "response_model" in route.keywords or "response_class" in route.keywords:
        return  # explicit response contract present
    path = f" {route.path}" if route.path else ""
    out.append(
        new_finding(
            "ROUTE_NO_RETURN_ANNOTATION",
            ctx.relpath,
            func.lineno,
            getattr(func.node, "col_offset", 0),
            (
                f"Route '{func.name}' ({route.method}{path}) has no return annotation and "
                "no response_model/response_class. FastAPI will serialize whatever "
                "is returned without validation or field filtering. Add a return "
                "annotation (-> XRead) or response_model if a response schema is intended."
            ),
            confidence="high" if route.fastapi_imported else "medium",
            symbol=f"{route.receiver}.{route.method.lower()}",
            function=func.qualname,
            environment=ctx.environment,
        )
    )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    for func in ctx.functions:
        _check_route_annotation(ctx, func, out)
        if not func.is_async:
            continue
        for call in iter_function_calls(func):
            _check_blocking(ctx, func, call, out)
            _check_nested_run(ctx, func, call, out)
    return out
