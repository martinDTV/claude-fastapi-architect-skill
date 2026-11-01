"""Span-exact edits of existing Python files (v2.2, increment 0): locators, guards and three primitives.

The additive transforms of ``transforms.py`` insert whole statements at the end of a module or a class body. The
increments of v2.2 need three more kinds of edit, each still an **insertion** that never re-renders existing code:

* ``insert_statement``: one statement inside a function body or at module level, after the last call to a named
  callee (``app.include_router``) or at the end of the body, at the anchor's indentation;
* ``insert_keyword_argument``: ``name=value`` added to one existing call (``include_router(r, prefix="/x")``);
* ``insert_parameter``: one parameter added to one function signature (``dep: X = Depends(y)``), where Python's
  ordering rules allow it.

Every primitive works on the exact source span of one node (``lineno`` / ``col_offset`` / ``end_lineno`` /
``end_col_offset``, exact since Python 3.8) and proves three things before returning: the new source parses and its
AST is the old AST plus exactly the intended node (compared with ``ast.dump``); every byte outside the edited span is
unchanged (compared literally, not inferred); the comment tokens of the file are the same before and after. A span
that contains a comment is refused rather than edited (``tokenize`` decides), an ambiguous target is refused, a
target that is already in the wanted state is a no-op with evidence, and a target inside a protected region is
refused. Formatting is preserved by construction outside the span; inside it the primitive follows the style it
finds (single line, or one argument per line with a trailing comma). Whether the result is stable under the
project's own formatter is a check for the verification copy (``detect_formatter`` / ``format_check_command``
prepare it); this module does not run anything.

What stays refused, by design: edits that would re-render a node (``ast.unparse`` loses comments and layout), edits
inside expressions other than adding an argument, renames across files, moving code. A concrete-syntax-tree
dependency would be the way to those, and the decision of v2.2 (ADR-2.2-04) is not to take it.
"""

from __future__ import annotations

import ast
import copy
import io
import re
import tokenize
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .transforms import StructuralEdit, TransformError, _diff, _parse

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# spans, regions and text helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Span:
    """A half-open source region: 1-based lines, 0-based columns (the ``ast`` convention)."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def to_dict(self) -> dict:
        return {"start_line": self.start_line, "start_col": self.start_col, "end_line": self.end_line, "end_col": self.end_col}


@dataclass(frozen=True)
class ProtectedRegion:
    """Lines an operator (or a baseline) declared off limits: 1-based, inclusive. Any edit whose span touches one is
    refused; the region is reported with its reason."""

    path: str
    start_line: int
    end_line: int
    reason: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "start_line": self.start_line, "end_line": self.end_line, "reason": self.reason}

    def touches(self, span: Span) -> bool:
        return not (span.end_line < self.start_line or span.start_line > self.end_line)


def node_span(node: ast.AST) -> Span:
    return Span(node.lineno, node.col_offset, getattr(node, "end_lineno", node.lineno), getattr(node, "end_col_offset", node.col_offset))


def _lines(source: str) -> List[str]:
    return source.splitlines(keepends=True)


def _offset(lines: List[str], line: int, col: int) -> int:
    """Character offset of (1-based line, 0-based column); columns are in characters for ``str`` sources."""
    return sum(len(text) for text in lines[: line - 1]) + col


def span_text(source: str, span: Span) -> str:
    lines = _lines(source)
    return source[_offset(lines, span.start_line, span.start_col):_offset(lines, span.end_line, span.end_col)]


def replace_span(source: str, span: Span, replacement: str) -> str:
    lines = _lines(source)
    start, end = _offset(lines, span.start_line, span.start_col), _offset(lines, span.end_line, span.end_col)
    return source[:start] + replacement + source[end:]


def bytes_outside_unchanged(old: str, new: str, span: Span) -> bool:
    """Everything before the span and everything after it is literally identical in ``new``."""
    lines = _lines(old)
    start, end = _offset(lines, span.start_line, span.start_col), _offset(lines, span.end_line, span.end_col)
    tail = len(old) - end
    return new[:start] == old[:start] and (tail == 0 or new[len(new) - tail:] == old[end:])


def newline_of(source: str) -> str:
    """The line separator the file uses (CRLF files stay CRLF)."""
    return "\r\n" if "\r\n" in source else "\n"


def _tokens(source: str) -> List[tokenize.TokenInfo]:
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise TransformError(f"cannot tokenize the source ({exc})") from exc


def comment_tokens(source: str) -> List[Tuple[int, int, str]]:
    return [(tok.start[0], tok.start[1], tok.string) for tok in _tokens(source) if tok.type == tokenize.COMMENT]


def comments_in_span(source: str, span: Span) -> List[Tuple[int, int, str]]:
    out = []
    for line, col, text in comment_tokens(source):
        after_start = (line, col) >= (span.start_line, span.start_col)
        before_end = (line, col) < (span.end_line, span.end_col)
        if after_start and before_end:
            out.append((line, col, text))
    return out


def _indent_of(line_text: str) -> str:
    return line_text[: len(line_text) - len(line_text.lstrip(" \t"))]


def _refuse_protected(path: str, span: Span, protected: Sequence[ProtectedRegion]) -> None:
    for region in protected:
        if region.path == path and region.touches(span):
            raise TransformError(f"{path}: the edit at lines {span.start_line}-{span.end_line} touches a protected region ({region.start_line}-{region.end_line}: {region.reason or 'declared by the operator'}); refused")


def _same_comments(path: str, old: str, new: str) -> None:
    """The comment tokens, in order and verbatim (their lines may shift after an insertion)."""
    if [t for _l, _c, t in comment_tokens(old)] != [t for _l, _c, t in comment_tokens(new)]:
        raise TransformError(f"{path}: the edit would change a comment; refused")


# --------------------------------------------------------------------------- #
# locators
# --------------------------------------------------------------------------- #


def dotted_name(node: ast.AST) -> Optional[str]:
    """``a.b.c`` for a Name / Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def find_function(tree: ast.Module, qualname: str, *, path: str = "<source>") -> ast.AST:
    """``name`` (top level) or ``Class.method``; refuses zero or several definitions."""
    parts = qualname.split(".")
    if len(parts) == 1:
        found = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == parts[0]]
    elif len(parts) == 2:
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parts[0]]
        if len(classes) != 1:
            raise TransformError(f"{path}: class {parts[0]!r} is defined {len(classes)} times; refused")
        found = [n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == parts[1]]
    else:
        raise TransformError(f"{path}: {qualname!r} is not a function or Class.method name")
    if not found:
        raise TransformError(f"{path}: function {qualname!r} not found; refused")
    if len(found) > 1:
        raise TransformError(f"{path}: function {qualname!r} is defined {len(found)} times; ambiguous target, refused")
    return found[0]


def _body_of(tree: ast.Module, function: Optional[str], path: str) -> Tuple[Optional[ast.AST], List[ast.stmt]]:
    if function is None:
        return None, tree.body
    owner = find_function(tree, function, path=path)
    return owner, owner.body  # type: ignore[attr-defined]


def _calls_in(statements: Iterable[ast.stmt], callee: str) -> List[Tuple[ast.stmt, ast.Call]]:
    """Calls to ``callee`` (dotted) that are the value of an expression statement directly in these statements."""
    out: List[Tuple[ast.stmt, ast.Call]] = []
    for stmt in statements:
        value = getattr(stmt, "value", None) if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return)) else None
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and dotted_name(value.func) == callee:
            out.append((stmt, value))
    return out


def find_call(tree: ast.Module, callee: str, *, function: Optional[str] = None, index: int = -1, first_argument: Optional[str] = None, path: str = "<source>") -> Tuple[ast.stmt, ast.Call]:
    """The statement-level call to ``callee`` inside ``function`` (or at module level): the last one by default, the
    n-th with ``index``, or the one whose first positional argument is the name ``first_argument`` (exactly one)."""
    _owner, body = _body_of(tree, function, path)
    calls = _calls_in(body, callee)
    where = f"in {function}" if function else "at module level"
    if first_argument is not None:
        calls = [(s, c) for s, c in calls if c.args and dotted_name(c.args[0]) == first_argument]
        if len(calls) != 1:
            raise TransformError(f"{path}: {len(calls)} call(s) to {callee}({first_argument}, ...) {where}; refused")
        return calls[0]
    if not calls:
        raise TransformError(f"{path}: no call to {callee!r} {where}; refused")
    try:
        return calls[index]
    except IndexError as exc:
        raise TransformError(f"{path}: {len(calls)} call(s) to {callee!r} {where}, index {index} does not exist; refused") from exc


# --------------------------------------------------------------------------- #
# the proof shared by the primitives
# --------------------------------------------------------------------------- #


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _module_dumps_without(tree: ast.Module, drop: ast.AST) -> List[str]:
    """``ast.dump`` of every top-level node, with ``drop`` removed wherever it sits (top level or inside a body)."""
    class _Dropper(ast.NodeTransformer):
        def generic_visit(self, node: ast.AST) -> ast.AST:  # type: ignore[override]
            for field_name, value in ast.iter_fields(node):
                if isinstance(value, list):
                    kept = [v for v in value if v is not drop]
                    setattr(node, field_name, [self.visit(v) if isinstance(v, ast.AST) else v for v in kept])
                elif isinstance(value, ast.AST):
                    setattr(node, field_name, self.visit(value))
            return node

    clone = copy.deepcopy(tree)
    # locate the clone of ``drop`` by position and dump
    target_dump, target_pos = _dump(drop), (getattr(drop, "lineno", None), getattr(drop, "col_offset", None))
    for node in ast.walk(clone):
        if (getattr(node, "lineno", None), getattr(node, "col_offset", None)) == target_pos and _dump(node) == target_dump:
            drop = node
            break
    return [_dump(n) for n in _Dropper().visit(clone).body]


# --------------------------------------------------------------------------- #
# primitive 1: a statement inside a body
# --------------------------------------------------------------------------- #


def insert_statement(path: str, source: str, code: str, *, function: Optional[str] = None, after_call: Optional[str] = None, protected: Sequence[ProtectedRegion] = ()) -> StructuralEdit:
    """Insert exactly one statement (``code``) into the body of ``function`` (``Class.method`` allowed; module level
    when None): after the last statement-level call to ``after_call`` when given, else at the end of the body. The
    statement takes the anchor's indentation. An identical statement already present in that body is a no-op."""
    tree = _parse(source, path)
    snippet = _parse(code.strip("\r\n"), f"<{path}>")
    if len(snippet.body) != 1:
        raise TransformError(f"{path}: the code to insert must be exactly one statement")
    new_dump = _dump(snippet.body[0])
    owner, body = _body_of(tree, function, path)
    if not body:
        raise TransformError(f"{path}: the target body is empty; refused")
    if any(_dump(stmt) == new_dump for stmt in body):
        return StructuralEdit(path=path, source=source, diff="", inserted=[], evidence={"already_present": True, "anchor": function or "module"})
    if after_call is not None:
        calls = _calls_in(body, after_call)
        if not calls:
            raise TransformError(f"{path}: no call to {after_call!r} in {function or 'the module'}; refused")
        anchor = calls[-1][0]
    else:
        anchor = body[-1]
    lines = _lines(source)
    end_line = getattr(anchor, "end_lineno", anchor.lineno)
    anchor_line = lines[anchor.lineno - 1]
    indent = _indent_of(anchor_line)
    nl = newline_of(source)
    code_lines = code.strip("\r\n").splitlines()
    insertion = "".join(f"{indent}{line}{nl}" if line.strip() else nl for line in code_lines)
    span = Span(end_line + 1, 0, end_line + 1, 0)
    _refuse_protected(path, Span(anchor.lineno, 0, end_line + 1, 0), protected)
    if end_line == len(lines) and not lines[-1].endswith(("\n", "\r\n")):
        lines[-1] = lines[-1] + nl  # the anchor was the last line without a newline
    new_source = "".join(lines[:end_line]) + insertion + "".join(lines[end_line:])
    new_tree = _parse(new_source, path)
    _owner2, new_body = _body_of(new_tree, function, path)
    if len(new_body) != len(body) + 1:
        raise TransformError(f"{path}: the body did not grow by exactly one statement; refused")
    inserted_nodes = [stmt for stmt in new_body if _dump(stmt) == new_dump]
    if len(inserted_nodes) != 1:
        raise TransformError(f"{path}: the inserted statement is not uniquely identifiable in the result; refused")
    if _module_dumps_without(new_tree, inserted_nodes[0]) != [_dump(n) for n in tree.body]:
        raise TransformError(f"{path}: the edit would alter existing code; refused")
    if not bytes_outside_unchanged(source, new_source, span) and not (end_line == len(_lines(source)) and new_source.startswith(source.rstrip("\r\n"))):
        raise TransformError(f"{path}: bytes outside the insertion changed; refused")
    _same_comments(path, source, new_source)
    evidence = {"anchor": f"after_last_call:{after_call}" if after_call else f"end_of_body:{function or 'module'}", "function": function, "inserted_at_line": end_line + 1, "span": span.to_dict(), "existing_nodes_unchanged": True, "bytes_outside_span_unchanged": True, "comments_unchanged": True}
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[code_lines[0].strip()[:80]], evidence=evidence)


# --------------------------------------------------------------------------- #
# primitive 2: a keyword argument on an existing call
# --------------------------------------------------------------------------- #


def _closing_paren(tokens: List[tokenize.TokenInfo], open_pos: Tuple[int, int]) -> Tuple[int, tokenize.TokenInfo]:
    """Index and token of the ``)`` matching the ``(`` at ``open_pos``."""
    depth = 0
    started = False
    for i, tok in enumerate(tokens):
        if not started:
            if tok.type == tokenize.OP and tok.string == "(" and tok.start == open_pos:
                started = True
                depth = 1
            continue
        if tok.type == tokenize.OP and tok.string in "([{":
            depth += 1
        elif tok.type == tokenize.OP and tok.string in ")]}":
            depth -= 1
            if depth == 0:
                return i, tok
    raise TransformError("unbalanced parentheses")


def _call_paren_tokens(source: str, call: ast.Call) -> Tuple[List[tokenize.TokenInfo], int, int]:
    """Tokens of the file plus the indexes of the call's opening and closing parentheses."""
    tokens = _tokens(source)
    func_end = (getattr(call.func, "end_lineno", call.lineno), getattr(call.func, "end_col_offset", call.col_offset))
    opening = [i for i, t in enumerate(tokens) if t.type == tokenize.OP and t.string == "(" and t.start >= func_end and t.start < (call.end_lineno, call.end_col_offset)]  # type: ignore[arg-type]
    if not opening:
        raise TransformError("call without parentheses")
    open_index = opening[0]
    close_index, _close = _closing_paren(tokens, tokens[open_index].start)
    return tokens, open_index, close_index


def _insert_in_parens(source: str, tokens: List[tokenize.TokenInfo], open_index: int, close_index: int, text: str) -> Tuple[str, str]:
    """Insert ``text`` as one more item of a parenthesised list, following the style found: ``(a, b)`` →
    ``(a, b, text)``; one item per line with a trailing comma → a new line ``text,``. Returns (new_source, style)."""
    nl = newline_of(source)
    lines = _lines(source)
    close = tokens[close_index]
    inner = [t for t in tokens[open_index + 1:close_index] if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
    close_offset = _offset(lines, close.start[0], close.start[1])
    if not inner:
        return source[:close_offset] + text + source[close_offset:], "empty"
    last = inner[-1]
    trailing_comma = last.type == tokenize.OP and last.string == ","
    close_line = lines[close.start[0] - 1]
    paren_alone = close_line[: close.start[1]].strip() == ""
    if paren_alone:
        previous = inner[-2] if trailing_comma and len(inner) >= 2 else last
        prev_line_text = lines[previous.end[0] - 1]
        indent = _indent_of(prev_line_text)
        insertion = f"{indent}{text},{nl}"
        line_start = _offset(lines, close.start[0], 0)
        if not trailing_comma:
            comma_at = _offset(lines, last.end[0], last.end[1])
            return source[:comma_at] + "," + source[comma_at:line_start] + insertion + source[line_start:], "multiline_added_trailing_comma"
        return source[:line_start] + insertion + source[line_start:], "multiline"
    if trailing_comma:
        return source[:close_offset] + f" {text}," + source[close_offset:], "inline_trailing_comma"
    return source[:close_offset] + f", {text}" + source[close_offset:], "inline"


def insert_keyword_argument(path: str, source: str, *, callee: str, keyword: str, value_code: str, function: Optional[str] = None, index: int = -1, first_argument: Optional[str] = None, protected: Sequence[ProtectedRegion] = ()) -> StructuralEdit:
    """Add ``keyword=value_code`` to one existing statement-level call (located like ``find_call``). The same keyword
    with the same value is a no-op; with another value it is refused (never overwritten); a call whose span holds a
    comment is refused."""
    if not _IDENT.match(keyword):
        raise TransformError(f"{path}: {keyword!r} is not a valid keyword name")
    value_node = ast.parse(value_code, mode="eval").body
    tree = _parse(source, path)
    stmt, call = find_call(tree, callee, function=function, index=index, first_argument=first_argument, path=path)
    span = node_span(call)
    _refuse_protected(path, span, protected)
    for kw in call.keywords:
        if kw.arg == keyword:
            if _dump(kw.value) == _dump(value_node):
                return StructuralEdit(path=path, source=source, diff="", inserted=[], evidence={"already_present": True, "call": callee, "keyword": keyword, "span": span.to_dict()})
            raise TransformError(f"{path}: {callee}(...) already has {keyword}={ast.unparse(kw.value)}; refused (never overwritten)")
    if any(kw.arg is None for kw in call.keywords):
        raise TransformError(f"{path}: {callee}(...) unpacks **kwargs; the keyword could collide at runtime, refused")
    comments = comments_in_span(source, span)
    if comments:
        raise TransformError(f"{path}: the call at line {span.start_line} contains a comment ({comments[0][2]!r}); refused rather than re-rendered")
    tokens, open_index, close_index = _call_paren_tokens(source, call)
    new_source, style = _insert_in_parens(source, tokens, open_index, close_index, f"{keyword}={value_code}")
    new_tree = _parse(new_source, path)
    new_stmt, new_call = find_call(new_tree, callee, function=function, index=index, first_argument=first_argument, path=path)
    expected_keywords = [_dump(k) for k in call.keywords] + [_dump(ast.keyword(arg=keyword, value=value_node))]
    if [_dump(k) for k in new_call.keywords] != expected_keywords or [_dump(a) for a in new_call.args] != [_dump(a) for a in call.args]:
        raise TransformError(f"{path}: the call did not gain exactly {keyword}=...; refused")
    old_without = [_dump(n) for n in tree.body]
    new_tree_copy = _parse(new_source, path)
    _s2, c2 = find_call(new_tree_copy, callee, function=function, index=index, first_argument=first_argument, path=path)
    c2.keywords = [k for k in c2.keywords if k.arg != keyword]
    if [_dump(n) for n in new_tree_copy.body] != old_without:
        raise TransformError(f"{path}: the edit would alter code outside the call; refused")
    edited = Span(span.start_line, span.start_col, span.end_line, span.end_col)
    if not bytes_outside_unchanged(source, new_source, edited):
        raise TransformError(f"{path}: bytes outside the call changed; refused")
    _same_comments(path, source, new_source)
    evidence = {"call": callee, "keyword": keyword, "style": style, "span": span.to_dict(), "existing_nodes_unchanged": True, "bytes_outside_span_unchanged": True, "comments_unchanged": True}
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[f"{callee}(..., {keyword}=...)"], evidence=evidence)


# --------------------------------------------------------------------------- #
# primitive 3: a parameter on an existing signature
# --------------------------------------------------------------------------- #


def _parse_parameter(parameter_code: str, path: str) -> Tuple[ast.arg, Optional[ast.expr]]:
    try:
        probe = ast.parse(f"def _probe({parameter_code}): pass")
    except SyntaxError as exc:
        raise TransformError(f"{path}: {parameter_code!r} is not a parameter ({exc.msg})") from exc
    args = probe.body[0].args  # type: ignore[attr-defined]
    if len(args.args) + len(args.kwonlyargs) != 1 or args.vararg or args.kwarg or args.posonlyargs:
        raise TransformError(f"{path}: exactly one plain parameter is accepted ({parameter_code!r})")
    if args.args:
        return args.args[0], (args.defaults[0] if args.defaults else None)
    return args.kwonlyargs[0], args.kw_defaults[0]


def _signature_parens(source: str, fn: ast.AST) -> Tuple[List[tokenize.TokenInfo], int, int]:
    tokens = _tokens(source)
    name_tok = [i for i, t in enumerate(tokens) if t.type == tokenize.NAME and t.string == fn.name and t.start >= (fn.lineno, 0)]  # type: ignore[attr-defined]
    for i in name_tok:
        if i + 1 < len(tokens) and tokens[i + 1].type == tokenize.OP and tokens[i + 1].string == "(" and i > 0 and tokens[i - 1].string == "def":
            close_index, _ = _closing_paren(tokens, tokens[i + 1].start)
            return tokens, i + 1, close_index
    raise TransformError(f"signature of {fn.name} not found")  # type: ignore[attr-defined]


def insert_parameter(path: str, source: str, *, function: str, parameter_code: str, protected: Sequence[ProtectedRegion] = ()) -> StructuralEdit:
    """Append one parameter to the signature of ``function`` (``Class.method`` allowed) where Python allows it: at
    the end of the keyword-only section when the signature has ``*``/``*args``, else at the end of the positional
    parameters, always before ``**kwargs``. A parameter without a default after one with a default is refused; a
    parameter with the same name is a no-op when identical and refused otherwise."""
    tree = _parse(source, path)
    fn = find_function(tree, function, path=path)
    new_arg, new_default = _parse_parameter(parameter_code, path)
    args = fn.args  # type: ignore[attr-defined]
    existing = {a.arg: a for a in args.posonlyargs + args.args + args.kwonlyargs}
    if new_arg.arg in existing or (args.vararg and args.vararg.arg == new_arg.arg) or (args.kwarg and args.kwarg.arg == new_arg.arg):
        current = existing.get(new_arg.arg)
        if current is not None and _dump(current) == _dump(new_arg):
            return StructuralEdit(path=path, source=source, diff="", inserted=[], evidence={"already_present": True, "function": function, "parameter": new_arg.arg})
        raise TransformError(f"{path}: {function} already has a parameter named {new_arg.arg!r}; refused")
    keyword_only = bool(args.vararg) or bool(args.kwonlyargs)
    if not keyword_only and new_default is None and args.defaults:
        raise TransformError(f"{path}: {function} has parameters with defaults; a parameter without a default cannot follow them (declare a default or a keyword-only signature); refused")
    header = Span(fn.lineno, fn.col_offset, fn.body[0].lineno, 0)  # type: ignore[attr-defined]
    _refuse_protected(path, header, protected)
    tokens, open_index, close_index = _signature_parens(source, fn)
    paren_span = Span(tokens[open_index].start[0], tokens[open_index].start[1], tokens[close_index].end[0], tokens[close_index].end[1])
    comments = comments_in_span(source, paren_span)
    if comments:
        raise TransformError(f"{path}: the signature of {function} contains a comment ({comments[0][2]!r}); refused rather than re-rendered")
    if args.kwarg is not None:
        # insert before **kwargs: treat the tokens up to the ``**`` as the list
        star_index = max(i for i in range(open_index + 1, close_index) if tokens[i].type == tokenize.OP and tokens[i].string == "**")
        # the item list ends at the token before ``**`` (a comma, usually)
        inner_close = star_index
        new_source, style = _insert_before_token(source, tokens, open_index, inner_close, parameter_code)
    else:
        new_source, style = _insert_in_parens(source, tokens, open_index, close_index, parameter_code)
    new_tree = _parse(new_source, path)
    new_fn = find_function(new_tree, function, path=path)
    old_args, new_args = fn.args, new_fn.args  # type: ignore[attr-defined]
    if keyword_only:
        ok = [_dump(a) for a in new_args.kwonlyargs] == [_dump(a) for a in old_args.kwonlyargs] + [_dump(new_arg)] and [_dump(a) for a in new_args.args] == [_dump(a) for a in old_args.args]
    else:
        ok = [_dump(a) for a in new_args.args] == [_dump(a) for a in old_args.args] + [_dump(new_arg)] and [_dump(a) for a in new_args.kwonlyargs] == [_dump(a) for a in old_args.kwonlyargs]
    ok = ok and [_dump(s) for s in new_fn.body] == [_dump(s) for s in fn.body] and [_dump(d) for d in new_fn.decorator_list] == [_dump(d) for d in fn.decorator_list]  # type: ignore[attr-defined]
    if not ok:
        raise TransformError(f"{path}: the signature did not gain exactly {new_arg.arg!r}; refused")
    clone = copy.deepcopy(new_tree)
    find_function(clone, function, path=path).args = fn.args  # type: ignore[attr-defined]
    if [_dump(n) for n in clone.body] != [_dump(n) for n in tree.body]:
        raise TransformError(f"{path}: the edit would alter code outside the signature of {function}; refused")
    if not bytes_outside_unchanged(source, new_source, paren_span):
        raise TransformError(f"{path}: bytes outside the signature changed; refused")
    _same_comments(path, source, new_source)
    evidence = {"function": function, "parameter": new_arg.arg, "keyword_only": keyword_only, "style": style, "span": paren_span.to_dict(), "existing_nodes_unchanged": True, "bytes_outside_span_unchanged": True, "comments_unchanged": True}
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[f"{function}({new_arg.arg})"], evidence=evidence)


def _insert_before_token(source: str, tokens: List[tokenize.TokenInfo], open_index: int, stop_index: int, text: str) -> Tuple[str, str]:
    """Insert ``text`` as one more item before the item that starts at ``stop_index`` (used for ``**kwargs``)."""
    lines = _lines(source)
    nl = newline_of(source)
    stop = tokens[stop_index]
    inner = [t for t in tokens[open_index + 1:stop_index] if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
    stop_offset = _offset(lines, stop.start[0], stop.start[1])
    if not inner:
        return source[:stop_offset] + f"{text}, " + source[stop_offset:], "before_kwargs_empty"
    stop_line = lines[stop.start[0] - 1]
    item_alone = stop_line[: stop.start[1]].strip() == ""
    if item_alone:
        indent = _indent_of(stop_line)
        line_start = _offset(lines, stop.start[0], 0)
        return source[:line_start] + f"{indent}{text},{nl}" + source[line_start:], "before_kwargs_multiline"
    return source[:stop_offset] + f"{text}, " + source[stop_offset:], "before_kwargs_inline"


# --------------------------------------------------------------------------- #
# formatter awareness (prepared here, executed by the verification copy in a later increment)
# --------------------------------------------------------------------------- #


def detect_formatter(pyproject_text: str) -> Optional[str]:
    """``ruff`` when ``[tool.ruff]`` or ``[tool.ruff.format]`` is declared, ``black`` for ``[tool.black]``, else None.
    Text-level detection on purpose: Python 3.9 has no ``tomllib`` and the question is only which tool to run."""
    if re.search(r"^\[tool\.ruff(\.format)?\]", pyproject_text, re.M):
        return "ruff"
    if re.search(r"^\[tool\.black\]", pyproject_text, re.M):
        return "black"
    return None


def format_check_command(formatter: str, paths: Sequence[str]) -> List[str]:
    """The argv that proves (or disproves) formatter stability of the edited files; nothing is run here."""
    if formatter == "ruff":
        return ["ruff", "format", "--check", *paths]
    if formatter == "black":
        return ["black", "--check", *paths]
    raise ValueError(f"unknown formatter {formatter!r}")


__all__ = ["ProtectedRegion", "Span", "bytes_outside_unchanged", "comment_tokens", "comments_in_span", "detect_formatter", "dotted_name", "find_call", "find_function", "format_check_command", "insert_keyword_argument", "insert_parameter", "insert_statement", "newline_of", "node_span", "replace_span", "span_text"]
