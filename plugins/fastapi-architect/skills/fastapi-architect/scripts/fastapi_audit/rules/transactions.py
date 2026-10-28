"""Transactions and concurrency: commits in loops, I/O inside transactions,
read-modify-write without a lock or version, swallowed commit failures."""

from __future__ import annotations

import ast
from typing import Dict, Iterator, List, Optional, Set

from ..kinds import CLIENT_KINDS, SESSION_KINDS, SESSION_LIKE_NAMES, function_kinds, receiver_kind, receiver_name
from ..model import ENV_MIGRATION, Finding, new_finding
from ..resolver import FunctionInfo, dotted_name, iter_scope_nodes
from . import FileContext
from .async_rules import HTTP_VERBS, iter_function_calls

CATEGORY = "transactions"

HTTP_MODULE_CALLS = frozenset(
    {f"requests.{v}" for v in HTTP_VERBS}
    | {f"httpx.{v}" for v in HTTP_VERBS}
    | {"httpx.stream", "urllib.request.urlopen", "aiohttp.request"}
)
SLEEP_CALLS = frozenset({"time.sleep", "asyncio.sleep", "anyio.sleep", "trio.sleep"})
CLIENT_LIKE_NAMES = frozenset({"client", "http", "http_client", "httpx_client", "api", "api_client", "requests_session"})
CLIENT_METHODS = frozenset(set(HTTP_VERBS) | {"send", "stream"})
LOADERS = frozenset({"get", "get_one", "scalar", "scalar_one", "scalar_one_or_none", "one", "one_or_none", "first"})
RMW_TEST = (
    "Open two sessions, load the same row in both, apply the update in each and commit both; "
    "with the current code the second commit silently overwrites the first. Repeat after the fix "
    "(SELECT ... FOR UPDATE, version_id_col / If-Match, or an atomic UPDATE ... SET x = x + :d) "
    "and assert the final value reflects both changes or that one writer gets a conflict."
)


def _session_receiver(call: ast.Call, kinds: Dict[str, str]) -> Optional[str]:
    """'high' when the receiver kind is a session, 'medium' when only its name suggests one."""
    kind = receiver_kind(call, kinds)
    if kind in SESSION_KINDS:
        return "high"
    name = receiver_name(call)
    if name in SESSION_LIKE_NAMES:
        return "medium"
    return None


def _calls_in(nodes: List[ast.AST]) -> Iterator[ast.Call]:
    for node in iter_scope_nodes(nodes):
        if isinstance(node, ast.Call):
            yield node


# --------------------------------------------------------------------------- #


def _commit_in_loop(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding]) -> None:
    reported: Set[int] = set()
    for node in func.scope_nodes():
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for call in _calls_in(node.body):
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "commit"):
                continue
            if id(call) in reported:
                break  # already reported for an enclosing loop
            confidence = _session_receiver(call, kinds)
            if confidence is None:
                continue
            reported.add(id(call))
            name = receiver_name(call)
            out.append(
                new_finding(
                    "TX_COMMIT_IN_LOOP",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"'{name}.commit()' runs inside a loop in '{func.qualname}' (loop at line "
                        f"{node.lineno}). Each iteration is its own transaction: a failure half-way "
                        "leaves earlier rows committed, and every commit is a round trip. If the "
                        "loop is one business operation, commit once after it; if batching is "
                        "deliberate, make the batch size explicit and the operation idempotent."
                    ),
                    confidence=confidence,
                    symbol="session.commit",
                    function=func.qualname,
                    environment=ctx.environment,
                    recommended_test="Make a later iteration fail and assert whether earlier rows should or should not remain committed.",
                )
            )
            break  # one finding per loop


def _external_call_in_transaction(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding]) -> None:
    for node in func.scope_nodes():
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        opens_tx = False
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr in {"begin", "begin_nested"}:
                opens_tx = True
        if not opens_tx:
            continue
        seen_lines: Set[int] = set()
        for call in _calls_in(node.body):
            dotted = func.resolve_node(call.func)
            attr = call.func.attr if isinstance(call.func, ast.Attribute) else None
            kind = receiver_kind(call, kinds)
            name = receiver_name(call)
            what: Optional[str] = None
            confidence = "medium"
            if dotted in HTTP_MODULE_CALLS:
                what, confidence = f"HTTP call '{dotted}'", "high"
            elif kind in CLIENT_KINDS and attr in CLIENT_METHODS:
                what, confidence = f"HTTP call '{name}.{attr}()'", "high"
            elif dotted in SLEEP_CALLS:
                what, confidence = f"sleep '{dotted}'", "medium"
            elif name in CLIENT_LIKE_NAMES and attr in CLIENT_METHODS:
                what, confidence = f"probable HTTP call '{name}.{attr}()'", "medium"
            if what is None or call.lineno in seen_lines:
                continue
            seen_lines.add(call.lineno)
            out.append(
                new_finding(
                    "TX_EXTERNAL_CALL_IN_TRANSACTION",
                    ctx.relpath,
                    call.lineno,
                    call.col_offset,
                    (
                        f"{what} happens inside the transaction block opened at line {node.lineno} in "
                        f"'{func.qualname}'. Row locks and the pooled connection are held for the whole "
                        "network round trip (and its timeout). Call the external system before or after "
                        "the transaction, or record the intent in an outbox table and send it from a worker."
                    ),
                    confidence=confidence,
                    symbol=dotted or f"{name}.{attr}",
                    function=func.qualname,
                    environment=ctx.environment,
                    recommended_test="Stall the external call (slow fake) and observe lock waits / pool exhaustion on concurrent requests.",
                )
            )


def _read_modify_write(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding]) -> None:
    body_calls = list(iter_function_calls(func))
    if any(isinstance(c.func, ast.Attribute) and c.func.attr == "with_for_update" for c in body_calls):
        return  # row lock taken somewhere in this function
    loaded: Dict[str, int] = {}
    for node in func.scope_nodes():
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        if not isinstance(value, ast.Call):
            continue
        chain_attrs: Set[str] = set()
        chain_names: Set[str] = set()
        cur: ast.AST = value
        while isinstance(cur, (ast.Call, ast.Attribute)):
            if isinstance(cur, ast.Call):
                cur = cur.func
                continue
            chain_attrs.add(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            chain_names.add(cur.id)
        session_like = any(kinds.get(n) in SESSION_KINDS or n in SESSION_LIKE_NAMES for n in chain_names)
        if session_like and chain_attrs & LOADERS:
            loaded[node.targets[0].id] = node.lineno
    if not loaded:
        return
    for node in func.scope_nodes():
        target: Optional[ast.Attribute] = None
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute):
            t = node.targets[0]
            if isinstance(t.value, ast.Name):
                # obj.x = obj.x + 1 / obj.x = f(obj.x)
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == t.value.id and sub.attr == t.attr:
                        target = t
                        break
        if target is None or not isinstance(target.value, ast.Name) or target.value.id not in loaded:
            continue
        obj = target.value.id
        out.append(
            new_finding(
                "TX_READ_MODIFY_WRITE",
                ctx.relpath,
                node.lineno,
                node.col_offset,
                (
                    f"'{obj}.{target.attr}' is updated from its previously loaded value (row loaded at "
                    f"line {loaded[obj]}) in '{func.qualname}' without a visible SELECT ... FOR UPDATE, "
                    "optimistic version check or atomic UPDATE. Under concurrent requests one of two "
                    "updates can be lost. Static analysis cannot show whether this path is ever "
                    "concurrent; reproduce with two transactions before adding locking."
                ),
                confidence="low",
                symbol=f"{obj}.{target.attr}",
                function=func.qualname,
                environment=ctx.environment,
                recommended_test=RMW_TEST,
            )
        )
        break  # one finding per function


def _commit_without_rollback(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding]) -> None:
    for node in func.scope_nodes():
        if not isinstance(node, ast.Try):
            continue
        # A commit inside a `with`/`async with` block within the try is bounded by that
        # context manager (session or transaction closed on exit); only commits on a
        # session that outlives the try can leave a failed transaction behind.
        bounded: Set[int] = set()
        for inner in iter_scope_nodes(node.body):
            if isinstance(inner, (ast.With, ast.AsyncWith)):
                bounded |= {id(c) for c in _calls_in(inner.body)}
        commits = [
            c for c in _calls_in(node.body)
            if isinstance(c.func, ast.Attribute) and c.func.attr == "commit" and _session_receiver(c, kinds) and id(c) not in bounded
        ]
        if not commits:
            continue
        for handler in node.handlers:
            has_rollback = any(isinstance(c.func, ast.Attribute) and c.func.attr == "rollback" for c in _calls_in(handler.body))
            reraises = any(isinstance(n, ast.Raise) for n in iter_scope_nodes(handler.body))
            if has_rollback or reraises:
                continue
            caught = ast.unparse(handler.type) if handler.type is not None and hasattr(ast, "unparse") else "Exception"
            out.append(
                new_finding(
                    "TX_COMMIT_WITHOUT_ROLLBACK",
                    ctx.relpath,
                    handler.lineno,
                    handler.col_offset,
                    (
                        f"The 'except {caught}' handler at line {handler.lineno} in '{func.qualname}' swallows a "
                        f"failure of the commit at line {commits[0].lineno} without rollback() or re-raise. "
                        "The session stays in a failed transaction and later statements raise "
                        "PendingRollbackError; roll back (or use 'with session.begin()') and decide "
                        "explicitly whether the caller must see the error."
                    ),
                    confidence="medium",
                    symbol="session.rollback",
                    function=func.qualname,
                    environment=ctx.environment,
                )
            )


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment == ENV_MIGRATION or ctx.alembic_imported:
        return out
    if not (ctx.sqlalchemy_imported or ctx.local_factories or (ctx.facts and ctx.facts.uses_sqlalchemy)):
        return out
    for func in ctx.functions:
        kinds = function_kinds(func, ctx.facts, ctx.local_factories)
        _commit_in_loop(ctx, func, kinds, out)
        _external_call_in_transaction(ctx, func, kinds, out)
        _read_modify_write(ctx, func, kinds, out)
        _commit_without_rollback(ctx, func, kinds, out)
    return out
