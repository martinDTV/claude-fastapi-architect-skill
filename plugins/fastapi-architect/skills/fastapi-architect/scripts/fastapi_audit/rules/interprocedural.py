"""Rules that need the call graph: blocking reached through synchronous helpers."""

from __future__ import annotations

from typing import List, Set

from ..model import ENV_APPLICATION, Finding, new_finding
from . import FileContext

CATEGORY = "async"


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    facts = ctx.facts
    graph = getattr(facts, "callgraph", None) if facts is not None else None
    if graph is None or ctx.environment != ENV_APPLICATION:
        # tests call synchronous fixtures and helpers routinely; only the direct rule applies there
        return out
    reported: Set[str] = set()  # one finding per blocking helper per file
    for func in ctx.functions:
        if not func.is_async:
            continue
        record = graph.record_for(ctx.relpath, func.qualname)
        if record is None:
            continue
        for site in record.calls:
            status, callee_key = graph.site_target(ctx.relpath, site.lineno, site.col)
            if status != "project" or not callee_key or callee_key in reported:
                continue
            callee = graph.functions.get(callee_key)
            if callee is None or callee.is_async:
                continue  # async callees report their own direct blocking calls
            path = graph.blocking_path(callee_key)
            if not path:
                continue
            chain, symbol = path
            reported.add(callee_key)
            depth = len(chain)
            call_path = [record.key, *chain, symbol]
            out.append(
                new_finding(
                    "ASYNC_BLOCKING_CALL_INDIRECT",
                    ctx.relpath,
                    site.lineno,
                    site.col,
                    (
                        f"'{func.qualname}' is async and calls '{callee.qualname}' ({callee.file}), a synchronous "
                        f"function that reaches '{symbol}' through {depth} resolved call{'s' if depth != 1 else ''}: "
                        + " -> ".join(call_path)
                        + ". The event loop blocks for the whole helper. Offload the helper "
                        "(anyio.to_thread.run_sync), make the chain async, or declare the route with plain 'def'."
                    ),
                    confidence="high" if depth == 1 else "medium",
                    symbol=symbol,
                    function=func.qualname,
                    environment=ctx.environment,
                    evidence="verified",
                    call_path=call_path,
                    recommended_test=(
                        "Run the async function concurrently with a trivial endpoint on one worker and measure the "
                        "trivial endpoint's latency while the helper executes."
                    ),
                )
            )
    return out
