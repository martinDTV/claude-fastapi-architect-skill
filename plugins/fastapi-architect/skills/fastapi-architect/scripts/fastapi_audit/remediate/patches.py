"""Structured patch proposals for the rules with a safe automatic recipe (v1.9 vertical slice).

Each recipe reads the finding (rule, file, line, column, details) and the current source of the file,
checks its preconditions on the AST, and returns either a ``PatchProposal`` (line-level edits plus a
unified diff) or a ``Blocked`` reason. The patched source must parse; otherwise the recipe is blocked.
Nothing here writes files.

Recipes:

* ``remove_commit_inside_begin`` (``TX_COMMIT_INSIDE_BEGIN_BLOCK``): delete the ``commit()`` statement
  inside ``with x.begin()``; ``pass`` keeps an otherwise empty block valid.
* ``add_await`` (``SA_ASYNC_SESSION_NOT_AWAITED``): insert ``await`` before the coroutine call when the
  innermost enclosing function is ``async def``; blocked inside synchronous functions, lambdas or
  comprehensions.
* ``move_commit_after_loop`` (``TX_COMMIT_IN_LOOP`` with ``loop_kind == per_item``): delete the per-item
  ``commit()`` that is a direct child of the loop body and insert the same statement after the loop, at
  the loop's indentation, unless the loop is already followed by a commit on the same receiver. Blocked
  when the loop contains ``return`` (an early return would then commit nothing, a behaviour change beyond
  atomicity) or when the commit is nested in ``if``/``try``/``with`` inside the loop.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from .model import Edit, PatchProposal

RECIPE_REMOVE_COMMIT_INSIDE_BEGIN = "remove_commit_inside_begin"
RECIPE_ADD_AWAIT = "add_await"
RECIPE_MOVE_COMMIT_AFTER_LOOP = "move_commit_after_loop"

RECIPES: Dict[str, str] = {
    "TX_COMMIT_INSIDE_BEGIN_BLOCK": RECIPE_REMOVE_COMMIT_INSIDE_BEGIN,
    "SA_ASYNC_SESSION_NOT_AWAITED": RECIPE_ADD_AWAIT,
    "TX_COMMIT_IN_LOOP": RECIPE_MOVE_COMMIT_AFTER_LOOP,
}


@dataclass
class Blocked:
    reason: str


def recipe_for(finding: Dict[str, object]) -> Optional[str]:
    """Recipe id for a finding dict, or None when the rule (or this shape of it) has no automatic recipe."""
    recipe = RECIPES.get(str(finding.get("rule")))
    if recipe == RECIPE_MOVE_COMMIT_AFTER_LOOP:
        details = finding.get("details") or {}
        if not isinstance(details, dict) or details.get("loop_kind") != "per_item" or details.get("policy"):
            return None  # deliberate loops reported under the opt-in policy are a design decision, not a patch
    return recipe


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _parents(tree: ast.AST) -> Dict[int, ast.AST]:
    out: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _call_at(tree: ast.AST, lineno: int, col: int, attr: Optional[str] = None) -> Optional[ast.Call]:
    best: Optional[ast.Call] = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.lineno == lineno and node.col_offset == col:
            if attr is None or (isinstance(node.func, ast.Attribute) and node.func.attr == attr):
                if best is None or (node.end_col_offset or 0) >= (best.end_col_offset or 0):
                    best = node
    return best


def _statement_of(node: ast.AST, parents: Dict[int, ast.AST]) -> Optional[ast.stmt]:
    cur: Optional[ast.AST] = node
    while cur is not None and not isinstance(cur, ast.stmt):
        cur = parents.get(id(cur))
    return cur if isinstance(cur, ast.stmt) else None


def _enclosing_function(node: ast.AST, parents: Dict[int, ast.AST]) -> Optional[ast.AST]:
    """Innermost ``def``/``async def`` around ``node`` (lambdas are not functions for this purpose)."""
    cur: Optional[ast.AST] = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return None


def _between(node: ast.AST, stop: ast.AST, parents: Dict[int, ast.AST]) -> List[ast.AST]:
    """Ancestors of ``node`` strictly below ``stop``."""
    out: List[ast.AST] = []
    cur = parents.get(id(node))
    while cur is not None and cur is not stop:
        out.append(cur)
        cur = parents.get(id(cur))
    return out


def _block_containing(stmt: ast.stmt, parents: Dict[int, ast.AST]) -> Tuple[Optional[ast.AST], Optional[List[ast.stmt]]]:
    owner = parents.get(id(stmt))
    if owner is None:
        return None, None
    for field_name in ("body", "orelse", "finalbody", "handlers"):
        block = getattr(owner, field_name, None)
        if isinstance(block, list) and any(s is stmt for s in block):
            return owner, block
    return owner, None


def _single_line_statement(lines: List[str], stmt: ast.stmt) -> bool:
    """The statement is the only thing on its (single) line: safe to delete or replace that line."""
    if stmt.lineno != getattr(stmt, "end_lineno", stmt.lineno):
        return False
    line = lines[stmt.lineno - 1]
    segment = line[stmt.col_offset:getattr(stmt, "end_col_offset", len(line))]
    rest = line[:stmt.col_offset].strip() + line[getattr(stmt, "end_col_offset", len(line)):].split("#", 1)[0].strip()
    return bool(segment.strip()) and rest == ""


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# --------------------------------------------------------------------------- #
# applying edits and building the proposal
# --------------------------------------------------------------------------- #


def apply_edits(source: str, edits: List[Edit]) -> str:
    """Apply line edits computed against ``source``; later lines first so earlier numbers stay valid."""
    lines = source.splitlines(keepends=True)
    newline = "\n"
    # highest line first; at the same line an insert_after is applied before a delete/replace so it lands right after the original line
    for edit in sorted(edits, key=lambda e: (e.lineno, 1 if e.kind == "insert_after" else 0), reverse=True):
        index = edit.lineno - 1
        if index < 0 or index >= len(lines):
            raise ValueError(f"edit outside the file: line {edit.lineno}")
        if edit.kind == "delete":
            del lines[index]
        elif edit.kind == "replace":
            lines[index] = edit.text.rstrip("\n") + newline
        elif edit.kind == "insert_after":
            lines.insert(index + 1, edit.text.rstrip("\n") + newline)
        else:
            raise ValueError(f"unknown edit kind {edit.kind!r}")
    return "".join(lines)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proposal(recipe: str, relpath: str, summary: str, source: str, edits: List[Edit]) -> Union[PatchProposal, Blocked]:
    try:
        patched = apply_edits(source, edits)
    except ValueError as exc:
        return Blocked(f"edit could not be applied: {exc}")
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        return Blocked(f"patched source does not parse: {exc.msg} (line {exc.lineno})")
    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True), patched.splitlines(keepends=True), fromfile=f"a/{relpath}", tofile=f"b/{relpath}", n=3
        )
    )
    if not diff:
        return Blocked("the recipe produced no change")
    return PatchProposal(recipe=recipe, file=relpath, summary=summary, edits=edits, diff=diff, original_sha256=_sha(source), patched_sha256=_sha(patched))


# --------------------------------------------------------------------------- #
# recipes
# --------------------------------------------------------------------------- #


def remove_commit_inside_begin(finding: Dict[str, object], source: str) -> Union[PatchProposal, Blocked]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Blocked(f"file does not parse: {exc.msg}")
    lines = source.splitlines(keepends=True)
    parents = _parents(tree)
    call = _call_at(tree, int(finding["line"]), int(finding.get("column") or 0), attr="commit")
    if call is None:
        return Blocked("commit() call not found at the reported position (file changed since the audit?)")
    stmt = _statement_of(call, parents)
    if not isinstance(stmt, ast.Expr):
        return Blocked("the commit is part of a larger statement; remove it by hand")
    if not _single_line_statement(lines, stmt):
        return Blocked("the commit does not stand alone on its line")
    _owner, block = _block_containing(stmt, parents)
    if block is None:
        return Blocked("could not locate the statement's block")
    line = lines[stmt.lineno - 1]
    if len(block) == 1:
        edits = [Edit(stmt.lineno, "replace", f"{_indent_of(line)}pass")]
    else:
        edits = [Edit(stmt.lineno, "delete")]
    return _proposal(RECIPE_REMOVE_COMMIT_INSIDE_BEGIN, str(finding["file"]), "remove the commit() inside the begin() block; the context manager commits on exit", source, edits)


def add_await(finding: Dict[str, object], source: str) -> Union[PatchProposal, Blocked]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Blocked(f"file does not parse: {exc.msg}")
    lines = source.splitlines(keepends=True)
    parents = _parents(tree)
    call = _call_at(tree, int(finding["line"]), int(finding.get("column") or 0))
    if call is None:
        return Blocked("call not found at the reported position (file changed since the audit?)")
    func = _enclosing_function(call, parents)
    if func is not None and any(isinstance(a, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for a in _between(call, func, parents)):
        return Blocked("the call is inside a lambda or comprehension; rewrite it as a statement first")
    if not isinstance(func, ast.AsyncFunctionDef):
        return Blocked("the call is not inside an 'async def'; make the function async or use a synchronous Session (manual recipe)")
    if any(isinstance(a, ast.Await) and a.value is call for a in _between(call, func, parents)):
        return Blocked("the call is already awaited")
    line = lines[call.lineno - 1]
    col = call.col_offset
    patched_line = line[:col] + "await " + line[col:]
    edits = [Edit(call.lineno, "replace", patched_line.rstrip("\n"))]
    return _proposal(RECIPE_ADD_AWAIT, str(finding["file"]), "await the coroutine method of the AsyncSession", source, edits)


def move_commit_after_loop(finding: Dict[str, object], source: str) -> Union[PatchProposal, Blocked]:
    details = finding.get("details") or {}
    if not isinstance(details, dict) or details.get("loop_kind") != "per_item":
        return Blocked("only per-item commit loops have an automatic recipe")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Blocked(f"file does not parse: {exc.msg}")
    lines = source.splitlines(keepends=True)
    parents = _parents(tree)
    call = _call_at(tree, int(finding["line"]), int(finding.get("column") or 0), attr="commit")
    if call is None:
        return Blocked("commit() call not found at the reported position (file changed since the audit?)")
    stmt = _statement_of(call, parents)
    if not isinstance(stmt, ast.Expr):
        return Blocked("the commit is part of a larger statement; move it by hand")
    if not _single_line_statement(lines, stmt):
        return Blocked("the commit does not stand alone on its line")
    loop = parents.get(id(stmt))
    if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)) or not any(s is stmt for s in loop.body):
        return Blocked("the commit is nested inside if/try/with within the loop; decide the transaction boundary by hand")
    if int(details.get("loop_line") or loop.lineno) != loop.lineno:
        return Blocked("the loop reported by the audit does not match the source (file changed since the audit?)")
    for node in ast.walk(loop):
        if isinstance(node, ast.Return):
            return Blocked("the loop contains 'return': moving the commit after it would commit nothing on early return")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return Blocked("the loop defines a function; move the commit by hand")
    func = _enclosing_function(stmt, parents)
    if func is None:
        return Blocked("the loop is not inside a function")
    _owner, block = _block_containing(loop, parents)
    if block is None:
        return Blocked("could not locate the loop's block")
    loop_end = getattr(loop, "end_lineno", None)
    if loop_end is None:
        return Blocked("the interpreter does not report end positions")
    receiver = ast.unparse(call.func.value) if hasattr(ast, "unparse") and isinstance(call.func, ast.Attribute) else None
    position = next(i for i, s in enumerate(block) if s is loop)
    following = block[position + 1] if position + 1 < len(block) else None
    already_committed = False
    if following is not None and isinstance(following, ast.Expr):
        inner = following.value.value if isinstance(following.value, ast.Await) else following.value
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "commit":
            already_committed = receiver is not None and hasattr(ast, "unparse") and ast.unparse(inner.func.value) == receiver
    commit_line = lines[stmt.lineno - 1]
    statement_text = commit_line.strip().split("#", 1)[0].rstrip()
    edits: List[Edit] = []
    if len(loop.body) == 1:
        edits.append(Edit(stmt.lineno, "replace", f"{_indent_of(commit_line)}pass"))
    else:
        edits.append(Edit(stmt.lineno, "delete"))
    if not already_committed:
        loop_indent = _indent_of(lines[loop.lineno - 1])
        edits.append(Edit(loop_end, "insert_after", f"{loop_indent}{statement_text}"))
    summary = "commit once after the loop instead of once per item" if not already_committed else "drop the per-item commit; the loop is already followed by a commit"
    return _proposal(RECIPE_MOVE_COMMIT_AFTER_LOOP, str(finding["file"]), summary, source, edits)


GENERATORS: Dict[str, Callable[[Dict[str, object], str], Union[PatchProposal, Blocked]]] = {
    RECIPE_REMOVE_COMMIT_INSIDE_BEGIN: remove_commit_inside_begin,
    RECIPE_ADD_AWAIT: add_await,
    RECIPE_MOVE_COMMIT_AFTER_LOOP: move_commit_after_loop,
}


def propose(finding: Dict[str, object], source: str) -> Union[PatchProposal, Blocked]:
    recipe = recipe_for(finding)
    if recipe is None:
        return Blocked("no automatic recipe for this rule or shape")
    return GENERATORS[recipe](finding, source)
