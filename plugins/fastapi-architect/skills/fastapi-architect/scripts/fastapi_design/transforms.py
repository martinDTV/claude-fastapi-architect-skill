"""Conservative structural edits of existing Python files (v2.0, increment 4).

Analysis is done with ``ast``; the edit is an **anchored insertion** into the original text at a line boundary
derived from the AST (end of the import block, end of a class body, end of the module). Everything that was there
stays byte-for-byte: formatting, comments and hand-written code are preserved because they are never rewritten.
The standard library is enough for additive edits; a concrete-syntax-tree library would only be justified for
rewriting existing nodes, which this increment does not do (and refuses).

Every transform returns a ``StructuralEdit`` with the new source, a unified diff and evidence: the inserted
symbol(s), the AST delta (the old top-level nodes must all survive unchanged, in order, plus the inserted ones),
and a syntax check of the result. Preconditions are refused explicitly: ambiguous targets (a class or router
bound more than once), duplicate symbols, duplicate routes (same method and path on the same router), a target
that does not exist, or a resulting source that fails to parse.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


class TransformError(ValueError):
    pass


@dataclass
class StructuralEdit:
    path: str
    source: str
    diff: str
    inserted: List[str] = field(default_factory=list)  # symbol names or import lines
    evidence: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {"path": self.path, "inserted": list(self.inserted), "evidence": dict(self.evidence), "diff": self.diff[:20000]}


def _parse(source: str, path: str) -> ast.Module:
    try:
        return ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise TransformError(f"{path}: cannot parse ({exc.msg} at line {exc.lineno})") from exc


def _dump_nodes(tree: ast.Module) -> List[str]:
    return [ast.dump(node, include_attributes=False) for node in tree.body]


def _diff(path: str, old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2))


def _lines(source: str) -> List[str]:
    return source.splitlines(keepends=True)


def _last_import_end(tree: ast.Module) -> int:
    """1-based line after the last top-level import at the head of the module (0 when there is none)."""
    end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = max(end, getattr(node, "end_lineno", node.lineno))
        elif end:
            break
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue  # module docstring
        else:
            break
    return end


def _verify_additive(path: str, old_tree: ast.Module, new_source: str, allowed_new: int) -> Tuple[ast.Module, Dict[str, object]]:
    """The new source parses and its top-level nodes are the old ones (unchanged, in order) plus the inserted ones.
    Inserted nodes may appear anywhere (imports at the head, functions at the end), so the check is a subsequence
    check on the dumps."""
    new_tree = _parse(new_source, path)
    old_dumps = _dump_nodes(old_tree)
    new_dumps = _dump_nodes(new_tree)
    i = 0
    for dump in new_dumps:
        if i < len(old_dumps) and dump == old_dumps[i]:
            i += 1
    if i != len(old_dumps):
        raise TransformError(f"{path}: the edit would alter existing top-level code; refused")
    added = len(new_dumps) - len(old_dumps)
    if added > allowed_new:
        raise TransformError(f"{path}: the edit added {added} top-level statements, {allowed_new} allowed; refused")
    return new_tree, {"top_level_before": len(old_dumps), "top_level_after": len(new_dumps), "existing_nodes_unchanged": True}


def module_names(tree: ast.Module) -> Dict[str, int]:
    """Top-level bound names -> how many times they are bound (functions, classes, assignments)."""
    counts: Dict[str, int] = {}
    for node in tree.body:
        names: List[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    return counts


def routes_on(tree: ast.Module, router: str) -> List[Tuple[str, str, str]]:
    """``(METHOD, path, function)`` for every ``@<router>.<method>(path)`` at module level."""
    out: List[Tuple[str, str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name) and dec.func.value.id == router:
                method = dec.func.attr.upper()
                path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str) else None
                if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS") and path is not None:
                    out.append((method, path, node.name))
    return out


def ensure_import(path: str, source: str, module: str, names: Sequence[str]) -> StructuralEdit:
    """Add ``from <module> import <names>`` after the last head import when the names are not imported yet."""
    tree = _parse(source, path)
    present: set = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == module and node.level == 0:
            present |= {a.asname or a.name for a in node.names}
    missing = [n for n in names if n not in present]
    if not missing:
        return StructuralEdit(path=path, source=source, diff="", inserted=[], evidence={"import_already_present": True})
    line = f"from {module} import {', '.join(missing)}\n"
    lines = _lines(source)
    at = _last_import_end(tree)
    if at == 0:  # no imports: after the docstring, or at the top
        at = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
            at = getattr(tree.body[0], "end_lineno", tree.body[0].lineno)
            line = "\n" + line
    new_lines = lines[:at] + [line] + lines[at:]
    new_source = "".join(new_lines)
    _, evidence = _verify_additive(path, tree, new_source, allowed_new=1)
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[line.strip()], evidence=evidence)


def insert_function(path: str, source: str, code: str, *, name: str, router: Optional[str] = None, method: Optional[str] = None, route_path: Optional[str] = None) -> StructuralEdit:
    """Append a top-level function (``code`` must define exactly one function named ``name``) at the end of the
    module, separated by two blank lines. Refuses a duplicate symbol, a router that is not bound exactly once, and a
    duplicate route (same method and path on that router)."""
    tree = _parse(source, path)
    snippet = _parse(code, f"<{name}>")
    if len(snippet.body) != 1 or not isinstance(snippet.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)) or snippet.body[0].name != name:
        raise TransformError(f"{path}: the code to insert must define exactly one function named {name!r}")
    counts = module_names(tree)
    if name in counts:
        raise TransformError(f"{path}: a top-level symbol named {name!r} already exists; refused (duplicate symbol)")
    if router is not None:
        bound = counts.get(router, 0)
        if bound == 0:
            raise TransformError(f"{path}: router {router!r} is not bound at module level; refused")
        if bound > 1:
            raise TransformError(f"{path}: router {router!r} is bound {bound} times; ambiguous target, refused")
        if method and route_path is not None:
            existing = [(m, p, f) for m, p, f in routes_on(tree, router) if m == method.upper() and p == route_path]
            if existing:
                raise TransformError(f"{path}: {method.upper()} {route_path} already exists on {router!r} ({existing[0][2]}); refused (duplicate endpoint)")
    body = source if source.endswith("\n") else source + "\n"
    new_source = body.rstrip("\n") + "\n\n\n" + code.strip("\n") + "\n"
    _, evidence = _verify_additive(path, tree, new_source, allowed_new=1)
    evidence.update({"anchor": "end_of_module", "symbol": name, "router": router, "route": f"{method.upper()} {route_path}" if method and route_path else None})
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[name], evidence=evidence)


def insert_class_field(path: str, source: str, class_name: str, field_line: str, *, field_name: str) -> StructuralEdit:
    """Append an annotated field at the end of a class body (after its last statement, at the body's indentation).
    Refuses when the class is not defined exactly once or the field already exists."""
    tree = _parse(source, path)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    if not classes:
        raise TransformError(f"{path}: class {class_name!r} not found; refused")
    if len(classes) > 1:
        raise TransformError(f"{path}: class {class_name!r} is defined {len(classes)} times; ambiguous target, refused")
    cls = classes[0]
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == field_name:
            raise TransformError(f"{path}: {class_name}.{field_name} already exists; refused (duplicate field)")
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == field_name for t in node.targets):
            raise TransformError(f"{path}: {class_name}.{field_name} already exists; refused (duplicate field)")
    last = cls.body[-1]
    end = getattr(last, "end_lineno", last.lineno)
    lines = _lines(source)
    indent = " " * (getattr(last, "col_offset", 4))
    insertion = f"{indent}{field_line.strip()}\n"
    new_lines = lines[:end] + [insertion] + lines[end:]
    new_source = "".join(new_lines)
    new_tree = _parse(new_source, path)
    # the class body grew by exactly one statement and every other top-level node is unchanged
    old_dumps = [ast.dump(n) for n in tree.body if n is not cls]
    new_classes = [n for n in new_tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    new_dumps = [ast.dump(n) for n in new_tree.body if not (isinstance(n, ast.ClassDef) and n.name == class_name)]
    if old_dumps != new_dumps or len(new_classes) != 1 or len(new_classes[0].body) != len(cls.body) + 1:
        raise TransformError(f"{path}: the edit would alter code outside {class_name}.{field_name}; refused")
    evidence = {"anchor": f"end_of_class_body:{class_name}", "class": class_name, "field": field_name, "existing_nodes_unchanged": True}
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[f"{class_name}.{field_name}"], evidence=evidence)


def compose(path: str, source: str, edits: Sequence) -> StructuralEdit:
    """Apply a sequence of transform callables ``f(path, source) -> StructuralEdit`` and merge the evidence."""
    current = source
    inserted: List[str] = []
    evidence: Dict[str, object] = {"steps": []}
    for step in edits:
        edit = step(path, current)
        current = edit.source
        inserted.extend(edit.inserted)
        evidence["steps"].append({"inserted": edit.inserted, **{k: v for k, v in edit.evidence.items() if k != "steps"}})  # type: ignore[union-attr]
    _parse(current, path)
    return StructuralEdit(path=path, source=current, diff=_diff(path, source, current), inserted=inserted, evidence=evidence)
