"""Opt-in three-way merge of hand-modified generated files (v2.2, increment 3): the planner's side.

``merge.py`` merges three texts and never touches a project. This module decides, per file the operator authorised in
``files.merge`` of an evolution contract, whether a merge may be *planned* at all and what evidence the plan carries:

* **BASE is authenticated, not assumed.** BASE is the file as the generator produced it at the project's baseline
  (regenerated from the contract the manifest records); it counts only when its digest equals the digest the manifest
  recorded for that file when it was written. A regenerated text that differs from the recorded digest (another tool
  version, a manipulated manifest, a contract that is not the one the project was generated from) means the base
  cannot be reconstructed, and the merge is refused with both digests.
* **LOCAL** is the file on disk (the developer's version); **NEW** is the deterministic output of the generator for the
  evolved contract. The three digests travel with the plan, the journal and the report.
* **Ownership is declared, not inferred.** The operator says where the generator may change lines
  (``generator_may_change``: the whole file or named regions), which regions the developer owns (the generator may not
  touch them) and which the generator owns (a manual change inside them is refused). Regions are a top-level
  function or class of BASE, a ``Class.method``, or a BASE line range. Everything else is merged hunk by hunk.
* **Conflicts are records, never markers.** Overlapping or same-point hunks, a node changed by both sides, a lost
  node, a duplicate symbol, import or router registration, a comment of the developer that would disappear, a
  line-ending change, a change outside the authorised region: each is a structured conflict with the base lines and
  the two sides' text, and the plan blocks. No file receives conflict markers and nothing is "resolved".

Nothing here reads or writes the project. ``verify_merged_file`` re-checks a merged file in the verification copy.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .merge import Hunk, MergeResult, hunks_between, merge_json, merge_python
from .spans import ProtectedRegion, comment_tokens, find_function, newline_of
from .transforms import TransformError

MERGE_TRANSFORMS = ("three_way_python", "three_way_json")
ON_CONFLICT = ("reject",)  # the only behaviour implemented: a conflict blocks the plan, nothing is written
OWNERSHIP_KEYS = ("generator_may_change", "developer_owns", "generator_owns")
GLOB_CHARS = "*?["
NEVER_MERGED = (".fastapi-architect/", "migrations/versions/")  # the manifest, the journal and historical revisions
MERGE_SCHEMA_NOTE = "files.merge needs schema fastapi-architect/evolution/1.5"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# the authorisation
# --------------------------------------------------------------------------- #


def _validate_region(region: Any, where: str, errors: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(region, dict) or len(region) != 1:
        errors.append(f"{where}: expected one of {{function: name}}, {{class: name}}, {{lines: [first, last]}}")
        return None
    (key, value), = region.items()
    if key in ("function", "class"):
        if not isinstance(value, str) or not value.strip() or any(ch in value for ch in GLOB_CHARS):
            errors.append(f"{where}.{key}: expected the exact name of a top-level {key} (or Class.method for a function)")
            return None
        return {key: value}
    if key == "lines":
        if not isinstance(value, list) or len(value) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in value) or value[0] < 1 or value[1] < value[0]:
            errors.append(f"{where}.lines: expected [first, last] with 1 <= first <= last (lines of the generated base)")
            return None
        return {"lines": [int(value[0]), int(value[1])]}
    errors.append(f"{where}.{key}: unknown region selector (known: function, class, lines)")
    return None


def validate_merge_authorizations(raw: Any, *, preserve: Sequence[str] = ()) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``files.merge[]`` entries: an exact path, a transformation, an ownership boundary and a conflict behaviour.
    Globs, directories, the manifest, the journal and historical revisions are refused; so is an entry that also
    appears under ``files.preserve`` (a preserved file is never regenerated, merged or otherwise)."""
    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, list) or not raw:
        return out, ["evolution.files.merge: expected a non-empty list of {path, transform, ownership?, on_conflict?} objects"]
    seen: set = set()
    for i, item in enumerate(raw):
        where = f"evolution.files.merge[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object")
            continue
        unknown = [key for key in item if key not in ("path", "transform", "ownership", "on_conflict") and not str(key).startswith("x-")]
        if unknown:
            errors.append(f"{where}.{unknown[0]}: unknown property (known: path, transform, ownership, on_conflict); nothing is assumed about what it means")
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"{where}.path: required, the exact relative path of one generated file")
            continue
        if any(ch in path for ch in GLOB_CHARS) or path.endswith("/"):
            errors.append(f"{where}.path: {path!r} is a pattern or a directory; merge authorisations name one exact file (too broad otherwise)")
            continue
        if path.startswith(("/", "\\")) or ".." in path.split("/") or "\\" in path:
            errors.append(f"{where}.path: {path!r} must stay inside the project (no absolute paths, no '..')")
            continue
        if path.startswith(NEVER_MERGED) or path.endswith("generation.json"):
            errors.append(f"{where}.path: {path!r} is never merged (the manifest, the journal and historical revisions are not merge targets)")
            continue
        if path in seen:
            errors.append(f"{where}.path: {path!r} appears twice")
            continue
        seen.add(path)
        if any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, g) for g in preserve):
            errors.append(f"{where}.path: {path!r} is also listed under files.preserve; a preserved file is never merged")
            continue
        transform = item.get("transform")
        if transform not in MERGE_TRANSFORMS:
            errors.append(f"{where}.transform: expected one of {', '.join(MERGE_TRANSFORMS)} (got {transform!r})")
            continue
        if transform == "three_way_python" and not path.endswith(".py"):
            errors.append(f"{where}.transform: three_way_python applies to .py files ({path!r})")
            continue
        if transform == "three_way_json" and not path.endswith(".json"):
            errors.append(f"{where}.transform: three_way_json applies to .json files ({path!r})")
            continue
        on_conflict = item.get("on_conflict", "reject")
        if on_conflict not in ON_CONFLICT:
            errors.append(f"{where}.on_conflict: only 'reject' is implemented (a conflict blocks the plan; nothing is resolved by policy); got {on_conflict!r}")
            continue
        ownership_raw = item.get("ownership", {})
        ownership: Dict[str, Any] = {"generator_may_change": "file", "developer_owns": [], "generator_owns": []}
        if not isinstance(ownership_raw, dict):
            errors.append(f"{where}.ownership: expected an object with generator_may_change, developer_owns, generator_owns")
            continue
        bad = False
        for key in ownership_raw:
            if key not in OWNERSHIP_KEYS and not str(key).startswith("x-"):
                errors.append(f"{where}.ownership.{key}: unknown property (known: {', '.join(OWNERSHIP_KEYS)})")
                bad = True
        may = ownership_raw.get("generator_may_change", "file")
        if may != "file":
            if not isinstance(may, list) or not may:
                errors.append(f"{where}.ownership.generator_may_change: 'file' or a non-empty list of regions")
                bad = True
            else:
                regions = [_validate_region(r, f"{where}.ownership.generator_may_change[{k}]", errors) for k, r in enumerate(may)]
                if any(r is None for r in regions):
                    bad = True
                else:
                    ownership["generator_may_change"] = regions
        for key in ("developer_owns", "generator_owns"):
            value = ownership_raw.get(key, [])
            if not isinstance(value, list):
                errors.append(f"{where}.ownership.{key}: expected a list of regions")
                bad = True
                continue
            regions = [_validate_region(r, f"{where}.ownership.{key}[{k}]", errors) for k, r in enumerate(value)]
            if any(r is None for r in regions):
                bad = True
            else:
                ownership[key] = regions
        if transform == "three_way_json" and (ownership["generator_may_change"] != "file" or ownership["developer_owns"] or ownership["generator_owns"]):
            errors.append(f"{where}.ownership: regions apply to Python files; a JSON document is merged key by key")
            bad = True
        if bad:
            continue
        out.append({"path": path, "transform": transform, "on_conflict": on_conflict, "ownership": ownership})
    return out, errors


# --------------------------------------------------------------------------- #
# regions over BASE
# --------------------------------------------------------------------------- #


def _node_lines(node: ast.AST) -> Tuple[int, int]:
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []) or []:
        start = min(start, decorator.lineno)
    return start, int(getattr(node, "end_lineno", node.lineno))


def resolve_regions(path: str, base: str, regions: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[int, int, str]], List[str]]:
    """``(first, last, label)`` BASE line ranges for each selector; errors name what could not be resolved."""
    out: List[Tuple[int, int, str]] = []
    errors: List[str] = []
    tree: Optional[ast.Module] = None
    if any("lines" not in r for r in regions):
        try:
            tree = ast.parse(base)
        except SyntaxError as exc:
            return out, [f"{path}: the generated base does not parse ({exc}); named regions cannot be resolved"]
    total = base.count("\n") + (0 if base.endswith("\n") or not base else 1)
    for region in regions:
        if "lines" in region:
            first, last = region["lines"]
            if last > max(total, 1):
                errors.append(f"{path}: lines {first}-{last} exceed the generated base ({total} lines)")
                continue
            out.append((first, last, f"lines {first}-{last}"))
        elif "function" in region:
            assert tree is not None
            try:
                node = find_function(tree, str(region["function"]), path=path)
            except TransformError as exc:
                errors.append(f"{path}: function {region['function']!r}: {exc}")
                continue
            first, last = _node_lines(node)
            out.append((first, last, f"function {region['function']}"))
        else:
            assert tree is not None
            matches = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == region["class"]]
            if len(matches) != 1:
                errors.append(f"{path}: class {region['class']!r}: {'not found' if not matches else 'ambiguous'} at the top level of the generated base")
                continue
            first, last = _node_lines(matches[0])
            out.append((first, last, f"class {region['class']}"))
    return out, errors


def _touches(hunk: Hunk, first: int, last: int) -> bool:
    start = hunk.base_start + 1
    end = hunk.base_end if not hunk.inserts else hunk.base_start + 1
    return not (end < first or start > last)


def _inside(hunk: Hunk, ranges: Sequence[Tuple[int, int, str]]) -> bool:
    start = hunk.base_start + 1
    end = hunk.base_end if not hunk.inserts else hunk.base_start + 1
    return any(first <= start and end <= last for first, last, _label in ranges)


# --------------------------------------------------------------------------- #
# structural checks on a merged Python text
# --------------------------------------------------------------------------- #


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def structural_problems(path: str, text: str) -> List[Dict[str, Any]]:
    """What a textually clean merge can still get wrong at the module level: a symbol defined twice, the same import
    twice, one name bound by two different imports, one router registered twice. Each is a conflict record."""
    problems: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [{"kind": "merged_text_does_not_parse", "path": path, "detail": str(exc), "base_lines": [exc.lineno or 0, exc.lineno or 0]}]
    seen_defs: Dict[str, int] = {}
    seen_imports: Dict[str, int] = {}
    bindings: Dict[str, Tuple[str, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen_defs:
                problems.append({"kind": "duplicate_symbol", "path": path, "detail": f"{node.name!r} is defined at lines {seen_defs[node.name]} and {node.lineno}", "base_lines": [seen_defs[node.name], node.lineno]})
            seen_defs.setdefault(node.name, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            key = _dump(node)
            if key in seen_imports:
                problems.append({"kind": "duplicate_import", "path": path, "detail": f"the same import appears at lines {seen_imports[key]} and {node.lineno}", "base_lines": [seen_imports[key], node.lineno]})
            seen_imports.setdefault(key, node.lineno)
            module = getattr(node, "module", None)
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                origin = f"{module}.{alias.name}" if isinstance(node, ast.ImportFrom) else alias.name
                if bound in bindings and bindings[bound][0] != origin:
                    problems.append({"kind": "conflicting_import", "path": path, "detail": f"{bound!r} is bound to {bindings[bound][0]} (line {bindings[bound][1]}) and to {origin} (line {node.lineno})", "base_lines": [bindings[bound][1], node.lineno]})
                bindings.setdefault(bound, (origin, node.lineno))
    registrations: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router" and node.args:
            prefix = next((_dump(k.value) for k in node.keywords if k.arg == "prefix"), "")
            target = f"{_dump(node.func.value)}:{_dump(node.args[0])}:{prefix}"  # the same router under the same prefix twice
            if target in registrations:
                problems.append({"kind": "duplicate_router_registration", "path": path, "detail": f"include_router({ast.unparse(node.args[0]) if hasattr(ast, 'unparse') else 'router'}) with the same prefix at lines {registrations[target]} and {node.lineno}", "base_lines": [registrations[target], node.lineno]})
            registrations.setdefault(target, node.lineno)
    return problems


def _headers(text: str) -> Optional[Dict[str, str]]:
    """``name -> dump of the header`` (decorators, name, signature, return annotation; the body stripped) of every
    top-level function and class."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            clone = ast.parse(ast.unparse(node)) if hasattr(ast, "unparse") else None
            header = {"name": node.name, "decorators": [_dump(d) for d in node.decorator_list], "kind": type(node).__name__}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                header["args"] = _dump(node.args)
                header["returns"] = _dump(node.returns) if node.returns is not None else None
            else:
                header["bases"] = [_dump(b) for b in node.bases]
                header["keywords"] = [_dump(k) for k in node.keywords]
            del clone
            out[node.name] = repr(sorted(header.items()))
    return out


def headers_changed_by_both(base: str, ours: str, theirs: str, merged: str) -> List[Dict[str, Any]]:
    """A top-level definition whose header (decorators, signature, bases) both sides changed is a conflict even when
    the lines do not overlap: the generated side and the developer disagree about what the thing *is*. A header one
    side changed must survive as that side wrote it."""
    b, o, t, m = _headers(base), _headers(ours), _headers(theirs), _headers(merged)
    problems: List[Dict[str, Any]] = []
    if b is None or o is None or t is None or m is None:
        return problems
    for name in sorted(set(b) & set(o) & set(t)):
        ours_changed, theirs_changed = o[name] != b[name], t[name] != b[name]
        if ours_changed and theirs_changed and o[name] != t[name]:
            problems.append({"kind": "signature_or_decorators_changed_by_both_sides", "name": name, "detail": f"{name!r}: the generator and the developer both changed its decorators, signature or bases, differently; the body merge cannot decide which header is meant"})
        elif ours_changed and m.get(name) != o[name]:
            problems.append({"kind": "header_not_preserved", "name": name, "detail": f"{name!r}: the header the generator wrote is not the one in the merged text"})
        elif theirs_changed and m.get(name) != t[name]:
            problems.append({"kind": "header_not_preserved", "name": name, "detail": f"{name!r}: the header the developer wrote is not the one in the merged text"})
    return problems


def _comment_texts(text: str) -> List[str]:
    try:
        return [c[2] for c in comment_tokens(text)]
    except (SyntaxError, ValueError):
        return []


def comments_lost(local: str, merged: str) -> List[str]:
    """Comments of the developer's file that the merged text no longer contains (by text, each occurrence counted)."""
    have = _comment_texts(merged)
    lost: List[str] = []
    for comment in _comment_texts(local):
        if comment in have:
            have.remove(comment)
        else:
            lost.append(comment)
    return lost


def manual_hunks(base: str, local: str) -> List[Hunk]:
    return hunks_between(base.splitlines(keepends=True), local.splitlines(keepends=True), "theirs")


def _subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    if not needle:
        return True
    n = len(needle)
    return any(list(haystack[i : i + n]) == list(needle) for i in range(len(haystack) - n + 1))


def manual_hunks_lost(base: str, local: str, merged: str) -> List[Dict[str, Any]]:
    """Every hunk the developer made over BASE whose replacement lines are not found, contiguous, in the merged text."""
    merged_lines = merged.splitlines(keepends=True)
    lost = []
    for hunk in manual_hunks(base, local):
        if hunk.lines and not _subsequence(list(hunk.lines), merged_lines):
            lost.append({"base_lines": [hunk.base_start + 1, max(hunk.base_end, hunk.base_start + 1)], "lines": list(hunk.lines)[:6]})
    return lost


# --------------------------------------------------------------------------- #
# the planned merge
# --------------------------------------------------------------------------- #


@dataclass
class MergeOutcome:
    path: str
    text: Optional[str] = None
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.text is not None and not self.conflicts

    def record(self) -> Dict[str, Any]:
        """What the plan shows for this file."""
        return {"path": self.path, "clean": self.clean, "conflicts": list(self.conflicts), **self.evidence}


def _snippet(lines: Sequence[str]) -> List[str]:
    return [line.rstrip("\r\n") for line in list(lines)[:6]]


def _conflict(kind: str, path: str, detail: str, base_lines: Sequence[int] = (0, 0), ours: Sequence[str] = (), theirs: Sequence[str] = ()) -> Dict[str, Any]:
    return {"kind": kind, "path": path, "detail": detail, "base_lines": list(base_lines), "generated": _snippet(ours), "manual": _snippet(theirs)}


def plan_merge(path: str, *, base: str, recorded_base_digest: Optional[str], local: str, new: str, authorization: Dict[str, Any], tool_version: str) -> MergeOutcome:
    """Decide whether the authorised file can be merged, and with what evidence. Conservative at every step: when the
    base cannot be authenticated, when a side touches a region it does not own, when the textual merge or the
    structural proof conflicts, when the merged text defines, imports or registers something twice, or when a
    comment or a hunk of the developer would be lost, the outcome is a list of conflicts and no text."""
    outcome = MergeOutcome(path=path)
    ownership = authorization.get("ownership") or {"generator_may_change": "file", "developer_owns": [], "generator_owns": []}
    regenerated = _sha(base)
    outcome.evidence = {
        "transform": authorization["transform"],
        "on_conflict": authorization.get("on_conflict", "reject"),
        "ownership": ownership,
        "base": {"sha256_recorded": recorded_base_digest, "sha256_regenerated": regenerated, "authenticated": recorded_base_digest == regenerated, "source": f"regenerated from the contract the manifest records (tool {tool_version}); authentic when equal to the digest the manifest recorded for the file"},
        "local_sha256": _sha(local),
        "new_sha256": _sha(new),
        "result_sha256": None,
    }
    if recorded_base_digest is None:
        outcome.conflicts.append(_conflict("merge_base_unknown", path, "the manifest records no digest for this file: it is not a generated file the tool can reconstruct"))
        return outcome
    if recorded_base_digest != regenerated:
        outcome.conflicts.append(_conflict("merge_base_not_reconstructible", path, f"the file regenerated from the manifest's contract ({regenerated[:12]}) is not the file the manifest recorded ({recorded_base_digest[:12]}): the base cannot be authenticated (another tool version, or a manifest that is not the one the file came from). Restore the file from version control and redo the manual change after the evolution, or regenerate the project with this version first"))
        return outcome
    if newline_of(local) != newline_of(base):
        outcome.conflicts.append(_conflict("line_endings_changed", path, f"the developer's file uses {newline_of(local)!r} line endings and the generated base {newline_of(base)!r}: every line differs, nothing can be merged; restore the base's line endings first"))
        return outcome
    if authorization["transform"] == "three_way_python":
        for label, side_text, kind in (("the developer's file", local, "local_does_not_parse"), ("the generated file", new, "generated_does_not_parse")):
            try:
                ast.parse(side_text)
            except SyntaxError as exc:
                outcome.conflicts.append(_conflict(kind, path, f"{label} does not parse: {exc.msg} (line {exc.lineno}); nothing is merged into or out of a file that is not valid Python", (exc.lineno or 0, exc.lineno or 0)))
        if outcome.conflicts:
            return outcome
    base_lines, new_lines, local_lines = base.splitlines(keepends=True), new.splitlines(keepends=True), local.splitlines(keepends=True)
    generated_hunks = hunks_between(base_lines, new_lines, "ours")
    developer_hunks = hunks_between(base_lines, local_lines, "theirs")
    outcome.evidence["hunks"] = {"generated": len(generated_hunks), "manual": len(developer_hunks)}
    protected: List[ProtectedRegion] = []
    if authorization["transform"] == "three_way_python":
        dev_regions, errors = resolve_regions(path, base, ownership.get("developer_owns") or [])
        gen_only, errors2 = resolve_regions(path, base, ownership.get("generator_owns") or [])
        may = ownership.get("generator_may_change", "file")
        allowed, errors3 = (resolve_regions(path, base, may) if may != "file" else ([], []))
        for message in errors + errors2 + errors3:
            outcome.conflicts.append(_conflict("merge_region_unresolved", path, message))
        if outcome.conflicts:
            return outcome
        protected = [ProtectedRegion(path, first, last, reason=f"developer_owns {label}") for first, last, label in dev_regions]
        for hunk in developer_hunks:
            for first, last, label in gen_only:
                if _touches(hunk, first, last):
                    outcome.conflicts.append(_conflict("manual_change_in_generator_region", path, f"the developer changed base lines {hunk.base_start + 1}-{max(hunk.base_end, hunk.base_start + 1)} inside {label}, a region the contract declares owned by the generator; the generator must be able to rewrite it: move the change out of the region or keep the file as generated", (hunk.base_start + 1, max(hunk.base_end, hunk.base_start + 1)), (), hunk.lines))
        if may != "file":
            for hunk in generated_hunks:
                if not _inside(hunk, allowed):
                    outcome.conflicts.append(_conflict("generated_change_outside_authorized_region", path, f"the generator changes base lines {hunk.base_start + 1}-{max(hunk.base_end, hunk.base_start + 1)}, outside the regions the contract authorises ({', '.join(label for _f, _l, label in allowed)})", (hunk.base_start + 1, max(hunk.base_end, hunk.base_start + 1)), hunk.lines, ()))
        if outcome.conflicts:
            return outcome
        result: MergeResult = merge_python(path, base, new, local, protected=protected)
    else:
        result = merge_json(base, new, local)
    outcome.evidence["merge"] = result.to_dict()
    for violation in result.protected_violations:
        outcome.conflicts.append(_conflict("generated_change_in_developer_region", path, f"the generator changes base lines {violation['base_lines'][0]}-{violation['base_lines'][1]} inside a region the contract declares owned by the developer ({violation['region'].get('reason')})", violation["base_lines"]))
    for c in result.conflicts:
        kind = {"overlap": "overlapping_changes", "same_point": "insertions_at_the_same_point", "lost_node": "structural_proof_failed", "json_value": "json_value_conflict", "protected": "generated_change_in_developer_region"}.get(c.kind, c.kind)
        outcome.conflicts.append(_conflict(kind, path, c.detail, (c.base_start + 1, max(c.base_end, c.base_start + 1)), c.ours, c.theirs))
    if outcome.conflicts or result.text is None:
        return outcome
    text = result.text
    if authorization["transform"] == "three_way_python":
        proof = result.evidence.get("structural_proof") if isinstance(result.evidence.get("structural_proof"), dict) else {}
        for problem in headers_changed_by_both(base, new, local, text):
            outcome.conflicts.append(_conflict(problem["kind"], path, problem["detail"]))
        outcome.evidence["nodes_changed_by_both_sides_in_distinct_lines"] = list(proof.get("both_modified", []))  # bodies merged hunk by hunk; headers checked above
        if result.evidence.get("duplicate_additions"):
            outcome.conflicts.append(_conflict("duplicate_addition", path, f"{result.evidence['duplicate_additions']} statement(s) added by both sides at different positions would appear twice"))
        outcome.conflicts.extend(structural_problems(path, text))
        for comment in comments_lost(local, text):
            outcome.conflicts.append(_conflict("manual_comment_lost", path, f"the developer's comment {comment!r} is not in the merged text"))
    for lost in manual_hunks_lost(base, local, text):
        outcome.conflicts.append(_conflict("manual_change_lost", path, f"the developer's change at base lines {lost['base_lines'][0]}-{lost['base_lines'][1]} is not in the merged text", lost["base_lines"], (), lost["lines"]))
    if outcome.conflicts:
        return outcome
    outcome.text = text
    outcome.evidence.update(
        result_sha256=_sha(text),
        preserved_manual_hunks=[{"base_lines": [h.base_start + 1, max(h.base_end, h.base_start + 1)], "lines": len(h.lines)} for h in developer_hunks],
        applied_generated_hunks=[{"base_lines": [h.base_start + 1, max(h.base_end, h.base_start + 1)], "lines": len(h.lines)} for h in generated_hunks],
        diff_local_to_result="\n".join(difflib.unified_diff(local.splitlines(), text.splitlines(), fromfile=f"local/{path}", tofile=f"merged/{path}", lineterm="", n=2))[:20000],
        checks_at_plan=["base authenticated", "line endings unchanged", "ownership boundary respected", "textual three-way merge clean", "structural proof (nodes of both sides present, none changed by both)", "no duplicate symbol, import or router registration", "developer's comments present", "developer's hunks present"] if authorization["transform"] == "three_way_python" else ["base authenticated", "line endings unchanged", "structural JSON merge clean", "developer's changes present"],
        validations_in_copy=["syntax and AST", "symbols and imports", "router registrations", "static audit", "public route contracts", "authorized tests", "protected files", "developer's hunks and comments present", "result digest equal to the plan"],
        authorization_required=["files.merge entry for this exact path (contract)", "--allow-merge on the command line"],
    )
    return outcome


def verify_merged_file(path: str, *, base: str, local: str, planned_result_sha256: Optional[str], on_disk: str, transform: str) -> Dict[str, Any]:
    """The checks of the verification copy for one merged file: the copy holds exactly the planned text, it parses,
    it has no duplicate symbol, import or registration, and the developer's hunks and comments are in it."""
    checks: Dict[str, Any] = {"path": path, "ok": True, "problems": []}
    actual = _sha(on_disk)
    checks["result_sha256"] = actual
    if planned_result_sha256 != actual:
        checks["problems"].append(f"the copy holds {actual[:12]}, the plan computed {str(planned_result_sha256)[:12]}")
    if transform == "three_way_python":
        try:
            ast.parse(on_disk)
            checks["parses"] = True
        except SyntaxError as exc:
            checks["parses"] = False
            checks["problems"].append(f"does not parse: {exc}")
        for problem in structural_problems(path, on_disk):
            checks["problems"].append(f"{problem['kind']}: {problem['detail']}")
        lost_comments = comments_lost(local, on_disk)
        if lost_comments:
            checks["problems"].append(f"comments of the developer missing: {lost_comments[:3]}")
    else:
        try:
            import json

            json.loads(on_disk)
            checks["parses"] = True
        except ValueError as exc:
            checks["parses"] = False
            checks["problems"].append(f"not valid JSON: {exc}")
    lost = manual_hunks_lost(base, local, on_disk)
    checks["manual_hunks"] = {"expected": len(manual_hunks(base, local)), "lost": lost}
    if lost:
        checks["problems"].append(f"{len(lost)} hunk(s) of the developer missing from the merged file")
    checks["ok"] = not checks["problems"]
    return checks


__all__ = ["MERGE_TRANSFORMS", "MergeOutcome", "ON_CONFLICT", "comments_lost", "manual_hunks_lost", "plan_merge", "resolve_regions", "structural_problems", "validate_merge_authorizations", "verify_merged_file"]
