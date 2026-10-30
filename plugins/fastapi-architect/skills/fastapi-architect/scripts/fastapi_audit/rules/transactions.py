"""Transactions and concurrency: commits in loops, I/O inside transactions,
read-modify-write without a lock or version, swallowed commit failures."""

from __future__ import annotations

import ast
from typing import Dict, Iterator, List, Optional, Set, Tuple

from ..kinds import CLIENT_KINDS, SESSION_KINDS, SESSION_LIKE_NAMES, function_kinds, function_kinds_ex, receiver_kind, receiver_name
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


def _receiver_evidence(call: ast.Call, kinds: Dict[str, str], evidence: Optional[Dict[str, str]]) -> str:
    """'verified' when the receiver's session kind is declared/resolved, else 'inferred'."""
    name = receiver_name(call)
    if name and kinds.get(name) in SESSION_KINDS:
        return (evidence or {}).get(name, "verified")
    return "inferred"


CHUNK_FUNCS = frozenset({"chunked", "batched", "chunks", "ichunked", "sliced", "partition", "grouper", "islice", "take"})


def _is_empty_test(test: ast.AST) -> bool:
    """``not rows`` / ``len(rows) == 0`` / ``len(rows) < n`` / ``rows is None`` / ``rows == []``: the loop ran out of data."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(right, ast.Constant) and right.value is None:
            return True
        if isinstance(op, ast.Eq) and isinstance(right, (ast.List, ast.Tuple)) and not right.elts:
            return True
        if isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "len" and isinstance(op, (ast.Lt, ast.LtE, ast.Eq)):
            return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_empty_test(v) for v in test.values)
    return False


def _counted_names(stmts: List[ast.stmt]) -> Set[str]:
    """Names incremented in the loop body (``n += 1``): batch counters."""
    return {n.target.id for n in iter_scope_nodes(stmts) if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and isinstance(n.op, ast.Add)}


def _is_size_test(test: ast.AST, counters: Set[str]) -> bool:
    """``len(buffer) >= N`` / ``n >= N`` (``n`` incremented in the loop): a batch reached its size."""
    for sub in ast.walk(test):
        if isinstance(sub, ast.Compare) and len(sub.ops) == 1 and isinstance(sub.ops[0], (ast.GtE, ast.Gt, ast.Eq)):
            left = sub.left
            if isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "len":
                return True
            if isinstance(left, ast.Name) and left.id in counters:
                return True
    return False


def _exits_under_if(stmts: List[ast.stmt], test: Optional[ast.AST] = None, out: Optional[List[ast.AST]] = None) -> List[ast.AST]:
    """Tests of the ``if`` branches that leave the loop (break/return), not entering nested loops or functions."""
    out = out if out is not None else []
    for stmt in stmts:
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.Break, ast.Return)) and test is not None:
            out.append(test)
            continue
        if isinstance(stmt, ast.If):
            _exits_under_if(stmt.body, stmt.test, out)
            _exits_under_if(stmt.orelse, test, out)
            continue
        for field_name in ("body", "orelse", "finalbody"):
            children = getattr(stmt, field_name, None)
            if isinstance(children, list):
                _exits_under_if([c for c in children if isinstance(c, ast.stmt)], test, out)
        for handler in getattr(stmt, "handlers", []) or []:
            _exits_under_if(handler.body, test, out)
    return out


def _guard_of(loop_body: List[ast.stmt], call: ast.Call) -> Optional[ast.If]:
    """Innermost ``if`` inside the loop whose body contains ``call``."""
    guards = [n for n in iter_scope_nodes(loop_body) if isinstance(n, ast.If) and any(c is call for c in _calls_in(n.body))]
    return max(guards, key=lambda n: n.lineno) if guards else None


def classify_commit_loop(node: ast.AST, call: ast.Call) -> Tuple[str, str]:
    """Structural kind of a commit inside a loop, with the evidence that decided it:

    * ``batch``: the commit is guarded by a modulo test, a buffer-size test (``len(buf) >= N`` or a counter
      incremented in the loop), or the loop iterates over chunks (``chunked(...)``, ``range(a, b, step)``);
    * ``paginated``: a ``while``/``range`` loop that stops when a fetched page is empty;
    * ``convergence``: a ``while``/``range`` loop that stops when a non-emptiness condition holds;
    * ``per_item``: none of the above. Names (``BATCH_SIZE``) prove nothing and are never consulted.
    """
    body = node.body
    guard = _guard_of(body, call)
    if guard is not None:
        if any(isinstance(b, ast.BinOp) and isinstance(b.op, ast.Mod) for b in ast.walk(guard.test)):
            return "batch", "commit guarded by a modulo test on the iteration counter"
        if _is_size_test(guard.test, _counted_names(body)):
            return "batch", "commit guarded by a buffer-size or counter threshold"
    if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.iter, ast.Call):
        fparts = dotted_name(node.iter.func) or []
        fname = fparts[-1] if fparts else ""
        if fname in CHUNK_FUNCS or (fname == "range" and len(node.iter.args) == 3):
            return "batch", f"loop iterates over chunks ({fname})"
    bounded = isinstance(node, ast.While) or (
        isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.iter, ast.Call) and (dotted_name(node.iter.func) or [""])[-1] in {"range", "count", "repeat"}
    )
    exits = _exits_under_if(body)
    if exits and bounded:
        if any(_is_empty_test(t) for t in exits):
            return "paginated", "loop stops when a fetched page is empty"
        return "convergence", "bounded loop that stops when a condition holds"
    return "per_item", "no batching, pagination or convergence structure around the commit"


DELIBERATE_LOOP_KINDS = frozenset({"batch", "paginated", "convergence"})


def _census(ctx: FileContext, kind: str) -> None:
    """Count every classified commit loop in the project facts, reported or not, so the classification is visible."""
    facts = ctx.facts
    if facts is not None and hasattr(facts, "commit_loops"):
        facts.commit_loops[kind] = facts.commit_loops.get(kind, 0) + 1


def _session_commits(stmts: List[ast.stmt], kinds: Dict[str, str]) -> List[ast.Call]:
    return [c for c in _calls_in(stmts) if isinstance(c.func, ast.Attribute) and c.func.attr == "commit" and _session_receiver(c, kinds) is not None]


POLICY_SINGLE_TX = "single_transaction_per_use_case"
LOWER_CONFIDENCE = {"high": "medium", "medium": "low", "low": "low"}


def _single_transaction_policy(ctx: FileContext) -> bool:
    """Opt-in ``transactions.single_transaction_per_use_case`` in ``.audit-fastapi.json`` (validated, default false)."""
    config = getattr(ctx.facts, "config", None) if ctx.facts is not None else None
    section = config.get("transactions") if isinstance(config, dict) else None
    return bool(isinstance(section, dict) and section.get(POLICY_SINGLE_TX) is True)


def _commit_in_loop(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding], evidence: Optional[Dict[str, str]] = None, skip: Optional[Set[int]] = None) -> None:
    reported: Set[int] = set()
    skip = skip or set()
    policy = _single_transaction_policy(ctx)
    for node in func.scope_nodes():
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        # A retry/attempt loop (try: ... commit ... break / except: ...) commits once on success,
        # and a worker tick loop (`while running: try: ... except: ...`) is one unit of work per
        # tick by design. Neither is "one transaction per item".
        tries = [s for s in node.body if isinstance(s, ast.Try)]
        if tries and len(node.body) == len(tries) + sum(1 for s in node.body if isinstance(s, (ast.Expr, ast.Assign)) and not isinstance(getattr(s, "value", None), ast.Call)):
            retry_like = any(isinstance(n, (ast.Break, ast.Return)) for t in tries for n in iter_scope_nodes(t.body))
            if retry_like or isinstance(node, ast.While):
                if _session_commits(node.body, kinds):
                    _census(ctx, "retry" if retry_like else "worker_tick")
                continue
        for call in _calls_in(node.body):
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "commit"):
                continue
            if id(call) in skip:
                continue  # reported as a commit inside a begin() block
            if id(call) in reported:
                break  # already reported for an enclosing loop
            confidence = _session_receiver(call, kinds)
            if confidence is None:
                continue
            reported.add(id(call))
            ev = _receiver_evidence(call, kinds, evidence)
            if ev == "inferred" and confidence == "high":
                confidence = "medium"
            # v1.8: the loop's structure decides whether the commit is accidental (per item) or deliberate.
            # Deliberate kinds (batch by modulo/size/chunks, pagination, convergence) are counted in
            # facts.commit_loops and not reported, like retry loops since v1.6; the message of a per-item
            # finding is unchanged, so its fingerprint is stable, and the kind and evidence live in details.
            loop_kind, loop_evidence = classify_commit_loop(node, call)
            _census(ctx, loop_kind)
            name = receiver_name(call)
            if loop_kind in DELIBERATE_LOOP_KINDS:
                if not policy:
                    reported.discard(id(call))
                    continue
                # opt-in: the project declares one transaction per use case, so a deliberate per-batch,
                # per-page or per-pass commit is a policy finding; a distinct message (no shared history)
                out.append(
                    new_finding(
                        "TX_COMMIT_IN_LOOP",
                        ctx.relpath,
                        call.lineno,
                        call.col_offset,
                        (
                            f"'{name}.commit()' runs inside a {loop_kind} loop in '{func.qualname}' (loop at line {node.lineno}; "
                            f"{loop_evidence}). The project declares transactions.single_transaction_per_use_case, so a commit per "
                            "batch, page or pass is reported even though the loop's structure shows it is deliberate: every "
                            "iteration is its own transaction and a failure half-way leaves earlier work committed. Commit once "
                            "after the loop, or document this loop as an exception."
                        ),
                        confidence=LOWER_CONFIDENCE[confidence],
                        symbol="session.commit",
                        function=func.qualname,
                        environment=ctx.environment,
                        evidence=ev,
                        recommended_test="Make a later iteration fail and assert whether earlier batches, pages or passes should remain committed under the project's policy.",
                        details={"loop_kind": loop_kind, "loop_evidence": loop_evidence, "loop_line": node.lineno, "policy": POLICY_SINGLE_TX},
                    )
                )
                break
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
                    evidence=ev,
                    recommended_test="Make a later iteration fail and assert whether earlier rows should or should not remain committed.",
                    details={"loop_kind": loop_kind, "loop_evidence": loop_evidence, "loop_line": node.lineno},
                )
            )
            break  # one finding per loop


def _commit_inside_begin(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding], evidence: Optional[Dict[str, str]] = None) -> Set[int]:
    """``x.commit()`` inside ``with x.begin():`` (v1.8). Returns the ids of the reported commit calls so the
    commit-in-loop rule does not report the same line again."""
    reported: Set[int] = set()

    def walk(stmts: List[ast.stmt], active: Set[str], block_body: Optional[List[ast.stmt]] = None) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            opened: Set[str] = set()
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "begin" and isinstance(expr.func.value, ast.Name):
                        opened.add(expr.func.value.id)
            if active:
                for node in ast.walk(stmt) if not isinstance(stmt, (ast.With, ast.AsyncWith)) else []:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "commit" and isinstance(node.func.value, ast.Name) and node.func.value.id in active and id(node) not in reported:
                        receiver = node.func.value.id
                        confidence = _session_receiver(node, kinds) or "medium"
                        ev = _receiver_evidence(node, kinds, evidence)
                        if ev == "inferred" and confidence == "high":
                            confidence = "medium"
                        reported.add(id(node))
                        _census(ctx, "inside_begin")
                        last = stmt is stmts[-1] and isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) and stmts is block_body
                        out.append(
                            new_finding(
                                "TX_COMMIT_INSIDE_BEGIN_BLOCK",
                                ctx.relpath,
                                node.lineno,
                                node.col_offset,
                                (
                                    f"'{receiver}.commit()' is called inside a 'with {receiver}.begin()' block in '{func.qualname}'. "
                                    "The context manager owns that transaction and commits it on exit; the explicit commit closes it early. "
                                    + (
                                        "It is the last statement of the block, so today it is only redundant; the next statement added "
                                        "after it (or the next loop iteration) will raise."
                                        if last else
                                        "Statements that follow it inside the block (including the next loop iteration) raise "
                                        "sqlalchemy.exc.InvalidRequestError: \"Can't operate on closed transaction inside context manager\" "
                                        "(reproduced on PostgreSQL with SQLAlchemy 2.0 in examples/tenant-ledger)."
                                    )
                                    + " Remove the inner commit (flush() if you need the SQL emitted) or drop the begin() block."
                                ),
                                confidence=confidence,
                                symbol=f"{receiver}.commit",
                                function=func.qualname,
                                environment=ctx.environment,
                                evidence=ev,
                                severity="medium" if last else None,
                                recommended_test=(
                                    "Run the block against PostgreSQL: the statement after the inner commit raises InvalidRequestError; "
                                    "without the inner commit every write of the block is visible together after exit."
                                ),
                                details={"follows": not last},
                            )
                        )
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, active | opened, stmt.body if opened else block_body)
                continue
            for field_name in ("body", "orelse", "finalbody"):
                children = getattr(stmt, field_name, None)
                if isinstance(children, list) and children and not active:
                    walk([c for c in children if isinstance(c, ast.stmt)], active, block_body)
            if not active:
                for handler in getattr(stmt, "handlers", []) or []:
                    walk(handler.body, active, block_body)

    walk(func.body, set())
    return reported


NETWORK_DEPTH = 2


def _reachable_network(graph, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[List[str], str]]:
    record = graph.functions.get(key)
    if record is None:
        return None
    if record.network_calls:
        return [key], record.network_calls[0][1]
    if depth >= NETWORK_DEPTH:
        return None
    visited = (visited or set()) | {key}
    for _site, callee in graph.edges.get(key, []):
        if callee in visited:
            continue
        sub = _reachable_network(graph, callee, depth + 1, visited)
        if sub:
            return [key, *sub[0]], sub[1]
    return None


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
            path_note = ""
            evidence = "verified"
            call_path = None
            if what is None:
                # v1.6: a project call inside the block whose callee reaches the network within two calls
                graph = getattr(ctx.facts, "callgraph", None) if ctx.facts is not None else None
                if graph is not None:
                    status, key = graph.site_target(ctx.relpath, call.lineno, call.col_offset)
                    reach = _reachable_network(graph, key) if (status == "project" and key) else None
                    if reach:
                        chain, symbol = reach
                        _k, edge_ev = graph.edge_info(ctx.relpath, call.lineno, call.col_offset)
                        evidence = "inferred" if edge_ev == "inferred" else "verified"
                        what, confidence = f"network call '{symbol}' reached through {' -> '.join(chain)}", "medium" if evidence == "verified" else "low"
                        call_path = [func.qualname, *chain, symbol]
                        path_note = " The call path is reported as evidence; read those functions in order."
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
                        "the transaction, or record the intent in an outbox table and send it from a worker." + path_note
                    ),
                    confidence=confidence,
                    symbol=dotted or f"{name}.{attr}",
                    function=func.qualname,
                    environment=ctx.environment,
                    evidence=evidence,
                    call_path=call_path,
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


COMMIT_FAILURE_TYPES = frozenset({
    "Exception", "BaseException", "SQLAlchemyError", "DBAPIError", "DatabaseError", "OperationalError", "IntegrityError",
    "InterfaceError", "InternalError", "ProgrammingError", "PendingRollbackError", "InvalidRequestError", "StatementError",
    "DataError", "NotSupportedError", "Error", "TimeoutError", "OSError", "ConnectionError",
})


def _catches_commit_failure(handler: ast.ExceptHandler) -> bool:
    """True for bare ``except``, ``except Exception`` and database/driver error families: the only
    handlers that can swallow a failed commit. Custom domain exceptions are not raised by commit()."""
    if handler.type is None:
        return True
    names = [n for n in ast.walk(handler.type) if isinstance(n, (ast.Name, ast.Attribute))]
    for n in names:
        label = n.id if isinstance(n, ast.Name) else n.attr
        if label in COMMIT_FAILURE_TYPES or label.endswith(("DBAPIError", "DatabaseError", "OperationalError", "IntegrityError", "SQLAlchemyError")):
            return True
    return False


def _commit_without_rollback(ctx: FileContext, func: FunctionInfo, kinds: Dict[str, str], out: List[Finding], evidence: Optional[Dict[str, str]] = None) -> None:
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
            if not _catches_commit_failure(handler):
                continue  # v1.6: a handler for a domain exception never sees the commit's DBAPI error
            caught = ast.unparse(handler.type) if handler.type is not None and hasattr(ast, "unparse") else "Exception"
            ev = _receiver_evidence(commits[0], kinds, evidence)
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
                    confidence="medium" if ev == "verified" else "low",
                    symbol="session.rollback",
                    function=func.qualname,
                    environment=ctx.environment,
                    evidence=ev,
                )
            )


def _nested_begin(ctx: FileContext, func: FunctionInfo, out: List[Finding]) -> None:
    """``with x.begin()`` opened inside a block that already began a transaction on ``x``."""

    def walk(stmts: List[ast.stmt], active: Set[str]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                opened: Set[str] = set()
                for item in stmt.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "begin" and isinstance(expr.func.value, ast.Name):
                        receiver = expr.func.value.id
                        if receiver in active:
                            out.append(
                                new_finding(
                                    "TX_NESTED_BEGIN",
                                    ctx.relpath,
                                    expr.lineno,
                                    expr.col_offset,
                                    (
                                        f"'{receiver}.begin()' is opened inside a block that already began a transaction on "
                                        f"'{receiver}' in '{func.qualname}'. SQLAlchemy raises InvalidRequestError ('A transaction "
                                        "is already begun on this Session'). Use begin_nested() for a savepoint, or let the outer "
                                        "transaction own this work."
                                    ),
                                    confidence="high",
                                    symbol=f"{receiver}.begin",
                                    function=func.qualname,
                                    environment=ctx.environment,
                                )
                            )
                        else:
                            opened.add(receiver)
                walk(stmt.body, active | opened)
                continue
            for field_name in ("body", "orelse", "finalbody", "handlers"):
                children = getattr(stmt, field_name, None)
                if isinstance(children, list) and children:
                    if field_name == "handlers":
                        for handler in children:
                            walk(handler.body, active)
                    else:
                        walk([c for c in children if isinstance(c, ast.stmt)], active)

    walk(func.body, set())


def run(ctx: FileContext) -> List[Finding]:
    out: List[Finding] = []
    if ctx.environment == ENV_MIGRATION or ctx.alembic_imported:
        return out
    if not (ctx.sqlalchemy_imported or ctx.local_factories or (ctx.facts and ctx.facts.uses_sqlalchemy)):
        return out
    for func in ctx.functions:
        kinds, evidence = function_kinds_ex(func, ctx.facts, ctx.local_factories, ctx.relpath)
        inside_begin = _commit_inside_begin(ctx, func, kinds, out, evidence)
        _commit_in_loop(ctx, func, kinds, out, evidence, skip=inside_begin)
        _external_call_in_transaction(ctx, func, kinds, out)
        _read_modify_write(ctx, func, kinds, out)
        _commit_without_rollback(ctx, func, kinds, out, evidence)
        _nested_begin(ctx, func, out)
    return out
