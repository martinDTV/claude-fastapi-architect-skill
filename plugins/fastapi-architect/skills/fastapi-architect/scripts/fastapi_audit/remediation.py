"""Verifiable remediation foundations (v1.8).

For a selected set of rules the auditor attaches a structured, *non-applied* remediation proposal to the
finding: the problem in one sentence, the evidence the proposal rests on, how certain the analysis is, the
proposed change as steps, the invariants the change must keep, the tests that would demonstrate it, and
the regression risk. Nothing here edits audited code, generates patches, or claims that anything was
verified: ``status`` is always ``proposed``, ``applied`` and ``verified`` are always ``False``. A later
tool may consume these blocks to apply and verify a change; this module only states what such a change
must prove.

The certainty is derived from the finding's evidence state: ``static_confirmed`` findings get
``high``; ``inferred`` ones ``medium``; ``insufficient``/``partial`` ones ``low`` and their proposal
starts with the verification step, because the first thing to do with a question is to answer it.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .model import Finding

REMEDIATION_SCHEMA = "1"
STATUS_PROPOSED = "proposed"

CERTAINTY_BY_STATE = {
    "reproduced": "high",
    "static_confirmed": "high",
    "inferred": "medium",
    "insufficient": "low",
    "partial": "low",
    "suppressed": "low",
}


def _base(finding: Finding, problem: str, fix: List[str], invariants: List[str], tests: List[str], risk: str, risk_why: str) -> Dict[str, object]:
    state = finding.evidence_state or "static_confirmed"
    certainty = CERTAINTY_BY_STATE.get(state, "low")
    if certainty == "low":
        fix = [
            "Confirm the finding first: the analysis could not establish it statically (see evidence); "
            "the steps below apply only once it is confirmed.",
            *fix,
        ]
    return {
        "schema": REMEDIATION_SCHEMA,
        "status": STATUS_PROPOSED,
        "applied": False,
        "verified": False,
        "problem": problem,
        "evidence": {
            "file": finding.file,
            "line": finding.line,
            "function": finding.function,
            "symbol": finding.symbol,
            "evidence": finding.evidence,
            "evidence_state": state,
            "call_path": list(finding.call_path) if finding.call_path else None,
            "details": dict(finding.details) if finding.details else None,
        },
        "certainty": certainty,
        "proposed_fix": fix,
        "invariants": invariants,
        "tests": tests,
        "regression_risk": {"level": risk, "why": risk_why},
    }


def _commit_in_loop(f: Finding) -> Dict[str, object]:
    details = f.details or {}
    loop_line = details.get("loop_line", "?")
    return _base(
        f,
        problem=f"'{f.function}' commits once per iteration of the loop at line {loop_line}; a failure half-way leaves earlier rows committed and each commit is a round trip.",
        fix=[
            "Decide the unit of work: if the loop is one business operation, move the commit after the loop (one transaction).",
            "If partial progress is wanted, batch explicitly: commit every N items under a visible size test, and make the per-item write idempotent so a retry after a failure cannot duplicate work.",
            "Keep the per-item flush() if later iterations read what earlier ones wrote.",
        ],
        invariants=[
            "Every row written before a failure is either rolled back (single transaction) or safely re-processable (explicit batches).",
            "The number of committed rows after a successful run is unchanged.",
        ],
        tests=[
            "Make a later iteration fail (patched repository or constraint violation) and assert the expected visibility of earlier rows from a second session.",
            "Count transactions during the loop (statement log or event listener) and assert the expected number (1, or ceil(items / N)).",
        ],
        risk="medium",
        risk_why="A single transaction holds locks longer and may hit statement or lock timeouts on large inputs; batching changes failure semantics the caller may rely on.",
    )


def _commit_inside_begin(f: Finding) -> Dict[str, object]:
    return _base(
        f,
        problem=(
            f"'{f.function}' calls {f.symbol}() inside a 'with {str(f.symbol).split('.')[0]}.begin()' block that already commits on exit; "
            + ("statements after it raise InvalidRequestError (closed transaction inside context manager)." if (f.details or {}).get("follows", True) else "it is redundant today and the next statement added to the block will raise.")
        ),
        fix=[
            "Remove the inner commit(); use flush() if the SQL must be emitted before later statements in the block.",
            "If an early commit is really intended, drop the begin() block and manage the transaction explicitly.",
        ],
        invariants=[
            "All statements of the block are committed together on normal exit and rolled back together on exception.",
            "No statement of the block runs against a closed transaction.",
        ],
        tests=[
            "Run the block against PostgreSQL with SQLAlchemy 2.0: assert no InvalidRequestError and that every write of the block is visible together after exit (examples/tenant-ledger/tests/test_antipatterns.py shows both halves).",
            "Raise after the former commit point inside the block and assert from a second session that nothing from the block is visible.",
        ],
        risk="low",
        risk_why="Removing a redundant commit inside a managed transaction does not change the successful path; it restores atomicity on the failure path and removes a crash on the following statement.",
    )


def _not_awaited(f: Finding) -> Dict[str, object]:
    return _base(
        f,
        problem=f"'{f.symbol}()' returns a coroutine that '{f.function}' never awaits, so the database operation never runs.",
        fix=[
            "Add 'await' in front of the call (or return the coroutine to an async caller that awaits it).",
            "If the function is synchronous, make it 'async def' and await it up the chain, or use a synchronous Session.",
        ],
        invariants=["The statement executes exactly once and its result is used.", "No 'coroutine was never awaited' RuntimeWarning is emitted."],
        tests=[
            "Run the function under 'python -W error::RuntimeWarning' and assert no warning.",
            "Assert the expected row/result from a second session after the call.",
        ],
        risk="low",
        risk_why="Awaiting a call that was silently skipped may surface errors that were hidden; that is the intended behaviour change.",
    )


def _blocking_indirect(f: Finding) -> Dict[str, object]:
    path = " -> ".join(f.call_path) if f.call_path else f.symbol or ""
    return _base(
        f,
        problem=f"'{f.function}' is async and reaches the blocking call '{f.symbol}' through {path}; the event loop stops for the whole helper.",
        fix=[
            "Offload the helper: 'await anyio.to_thread.run_sync(helper, ...)' (or 'asyncio.to_thread'), keeping its signature.",
            "Or make the chain async: replace the blocking library call with its async counterpart (httpx.AsyncClient, asyncio.sleep, asyncpg/async SQLAlchemy) and 'await' every step.",
            "Or declare the route with plain 'def' so FastAPI runs it in the threadpool.",
        ],
        invariants=["The helper's return value and exceptions are unchanged.", "No synchronous network, sleep or subprocess call remains on the async path."],
        tests=[
            "Run the function concurrently with a trivial endpoint on one worker and assert the trivial endpoint's latency stays below the helper's duration.",
            "Unit-test the helper in isolation to show its behaviour did not change after offloading.",
        ],
        risk="medium",
        risk_why="Thread offloading changes context (contextvars, thread-local sessions); making a chain async touches every caller.",
    )


def _resource_authz(f: Finding) -> Dict[str, object]:
    return _base(
        f,
        problem=f"'{f.function}' acts on '{f.symbol}' for an authenticated caller without a visible check that the resource belongs to that caller.",
        fix=[
            "Load the resource through a dependency that receives both the principal and the id and raises 404/403 when they do not match (resource dependency).",
            "Or scope the repository call by the principal/tenant (WHERE owner_id = :principal) so a foreign id returns nothing.",
            "Or document the deliberate exception with the project's suppression marker and the reason.",
        ],
        invariants=["A caller cannot read or change a resource owned by another principal, in any route that reaches the same service call."],
        tests=[
            "Authenticate as principal A, call the route with a resource id of principal B and assert 403 or 404 and no side effect.",
            "Repeat for every verb on the same path.",
        ],
        risk="low",
        risk_why="Adding an ownership check only narrows access; the risk is breaking legitimate shared-resource flows, which the second test reveals.",
    )


REMEDIATIONS: Dict[str, Callable[[Finding], Dict[str, object]]] = {
    "TX_COMMIT_IN_LOOP": _commit_in_loop,
    "TX_COMMIT_INSIDE_BEGIN_BLOCK": _commit_inside_begin,
    "SA_ASYNC_SESSION_NOT_AWAITED": _not_awaited,
    "ASYNC_BLOCKING_CALL_INDIRECT": _blocking_indirect,
    "SEC_RESOURCE_AUTHZ_UNVERIFIED": _resource_authz,
}


def build_remediation(finding: Finding) -> Optional[Dict[str, object]]:
    """Structured proposal for the selected rules; None for every other rule and for suppressed findings."""
    builder = REMEDIATIONS.get(finding.rule)
    if builder is None or finding.suppressed:
        return None
    return builder(finding)
