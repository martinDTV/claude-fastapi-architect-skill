"""Pure three-way merge (v2.2, increment 0): base, ours (regenerated) and theirs (the file with manual changes).

Nothing here reads or writes a project; the functions take three texts and return a ``MergeResult``. The planner
of a later increment decides which files may be merged (opt-in, per file or region) and verifies the result in a
copy before anything lands.

Algorithm (``merge3``): the line hunks of base→ours and base→theirs (``difflib.SequenceMatcher`` opcodes); hunks
whose base ranges do not overlap are both applied; two hunks that overlap, or two insertions at the same base
position, are a **conflict** unless they are identical (then the change is applied once); a conflict never
produces a text. Lines keep their own endings, so CRLF files merge as CRLF. **Protected regions** (base line ranges
an operator declared off limits) refuse any hunk of *ours* that touches them: the generated side may not rewrite
what the operator froze; the manual side owns its own lines.

Python files (``merge_python``) get a structural proof on top: the merged text parses, every top-level node that
*ours* added over base is present, every node that *theirs* added over base is present (``ast.dump`` of the nodes);
a merge that loses either side is a conflict even when it is textually clean. A statement added by both sides at
different positions is reported as ``duplicate_additions`` for the verification to judge (two mounts of the same
router are a route problem the copy will show, not a merge problem).

JSON documents (``merge_json``, for ``.audit-fastapi.json``) merge structurally: a key changed on one side only
takes that side; both sides changing it to the same value is one change; to different values is a conflict named
by its JSON path; nested objects recurse; arrays are compared whole (changed on one side only: taken; on both:
conflict), because an array of route prefixes has an order the tool does not know how to merge.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .spans import ProtectedRegion


@dataclass(frozen=True)
class Hunk:
    side: str  # ours | theirs
    base_start: int  # 0-based, inclusive
    base_end: int  # 0-based, exclusive (== base_start for a pure insertion)
    lines: Tuple[str, ...]  # replacement lines, endings kept

    def to_dict(self) -> Dict[str, Any]:
        return {"side": self.side, "base_lines": [self.base_start + 1, self.base_end], "replacement_lines": len(self.lines)}

    @property
    def inserts(self) -> bool:
        return self.base_start == self.base_end


@dataclass
class Conflict:
    kind: str  # overlap | same_point | lost_node | json_value | protected
    base_start: int
    base_end: int
    ours: Tuple[str, ...] = ()
    theirs: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "base_lines": [self.base_start + 1, self.base_end], "ours": list(self.ours), "theirs": list(self.theirs), "detail": self.detail}


@dataclass
class MergeResult:
    text: Optional[str]  # None whenever a conflict exists
    conflicts: List[Conflict] = field(default_factory=list)
    applied: Dict[str, int] = field(default_factory=lambda: {"ours": 0, "theirs": 0, "identical": 0})
    protected_violations: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.text is not None and not self.conflicts and not self.protected_violations

    def to_dict(self) -> Dict[str, Any]:
        return {"clean": self.clean, "conflicts": [c.to_dict() for c in self.conflicts], "applied": dict(self.applied), "protected_violations": list(self.protected_violations), "evidence": dict(self.evidence)}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split(text: str) -> List[str]:
    return text.splitlines(keepends=True)


def hunks_between(base: Sequence[str], other: Sequence[str], side: str) -> List[Hunk]:
    """Non-equal opcodes of a line diff, as hunks over the base."""
    matcher = difflib.SequenceMatcher(a=list(base), b=list(other), autojunk=False)
    out: List[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.append(Hunk(side=side, base_start=i1, base_end=i2, lines=tuple(other[j1:j2])))
    return out


def _overlap(a: Hunk, b: Hunk) -> bool:
    """Two insertions at one point conflict (their order is unknown); an insertion strictly inside a replaced range
    conflicts; an insertion at the edge of a replaced range does not (it lands before or after the block)."""
    if a.inserts and b.inserts:
        return a.base_start == b.base_start
    if a.inserts:
        return b.base_start < a.base_start < b.base_end
    if b.inserts:
        return a.base_start < b.base_start < a.base_end
    return a.base_start < b.base_end and b.base_start < a.base_end


def merge3(base: str, ours: str, theirs: str, *, protected: Sequence[ProtectedRegion] = (), path: str = "") -> MergeResult:
    """Line-level three-way merge; ``text`` is None when any conflict or protected violation exists."""
    base_lines, ours_lines, theirs_lines = _split(base), _split(ours), _split(theirs)
    result = MergeResult(text=None)
    result.evidence = {"path": path, "base_sha256": _digest(base), "ours_sha256": _digest(ours), "theirs_sha256": _digest(theirs), "base_lines": len(base_lines)}
    if ours == theirs:
        result.text = ours
        result.evidence["identical_sides"] = True
        return result
    if theirs == base:
        result.text = ours
        result.applied["ours"] = len(hunks_between(base_lines, ours_lines, "ours"))
        result.evidence["theirs_unchanged"] = True
        return result
    if ours == base:
        result.text = theirs
        result.applied["theirs"] = len(hunks_between(base_lines, theirs_lines, "theirs"))
        result.evidence["ours_unchanged"] = True
        return result
    ours_hunks = hunks_between(base_lines, ours_lines, "ours")
    theirs_hunks = hunks_between(base_lines, theirs_lines, "theirs")
    for hunk in ours_hunks:
        for region in protected:
            if region.path and path and region.path != path:
                continue
            touched = range(hunk.base_start + 1, (hunk.base_end if not hunk.inserts else hunk.base_start + 1) + 1)
            if any(region.start_line <= line <= region.end_line for line in touched):
                result.protected_violations.append({"base_lines": [hunk.base_start + 1, max(hunk.base_end, hunk.base_start + 1)], "region": region.to_dict()})
    chosen: List[Hunk] = []
    used_theirs: set = set()
    for o in ours_hunks:
        partners = [t for t in theirs_hunks if _overlap(o, t)]
        if not partners:
            chosen.append(o)
            continue
        if len(partners) == 1 and partners[0].base_start == o.base_start and partners[0].base_end == o.base_end and partners[0].lines == o.lines:
            chosen.append(o)
            used_theirs.add(id(partners[0]))
            result.applied["identical"] += 1
            continue
        start = min([o.base_start] + [t.base_start for t in partners])
        end = max([o.base_end] + [t.base_end for t in partners])
        theirs_text = tuple(line for t in partners for line in t.lines)
        kind = "same_point" if o.inserts and all(t.inserts for t in partners) else "overlap"
        result.conflicts.append(Conflict(kind=kind, base_start=start, base_end=end, ours=o.lines, theirs=theirs_text, detail=f"ours changes base lines {o.base_start + 1}-{max(o.base_end, o.base_start + 1)}, theirs changes {start + 1}-{max(end, start + 1)}"))
        for t in partners:
            used_theirs.add(id(t))
    for t in theirs_hunks:
        if id(t) not in used_theirs:
            chosen.append(t)
    if result.conflicts or result.protected_violations:
        result.evidence["hunks"] = {"ours": len(ours_hunks), "theirs": len(theirs_hunks)}
        return result
    chosen.sort(key=lambda h: (h.base_start, 0 if h.inserts else 1, 0 if h.side == "theirs" else 1))
    out: List[str] = []
    cursor = 0
    for hunk in chosen:
        out.extend(base_lines[cursor:hunk.base_start])
        out.extend(hunk.lines)
        cursor = max(cursor, hunk.base_end)
    out.extend(base_lines[cursor:])
    result.text = "".join(out)
    result.applied["ours"] = sum(1 for h in chosen if h.side == "ours") - result.applied["identical"]
    result.applied["theirs"] = sum(1 for h in chosen if h.side == "theirs")
    result.evidence["hunks"] = {"ours": len(ours_hunks), "theirs": len(theirs_hunks)}
    result.evidence["result_sha256"] = _digest(result.text)
    return result


def _top_level_dumps(source: str) -> Optional[List[str]]:
    try:
        return [ast.dump(n, include_attributes=False) for n in ast.parse(source).body]
    except SyntaxError:
        return None


def _node_key(node: ast.AST, dump: str) -> Tuple[str, str]:
    """What identifies a top-level statement across versions: a definition by its name, an assignment by its
    targets, anything else (imports, expressions) by its own text."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ("def", node.name)
    if isinstance(node, ast.Assign):
        names = tuple(sorted(t.id for t in node.targets if isinstance(t, ast.Name)))
        if names:
            return ("assign", ",".join(names))
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return ("assign", node.target.id)
    return ("stmt", dump)


def _keyed(source: str) -> Optional[Dict[Tuple[str, str], List[str]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    out: Dict[Tuple[str, str], List[str]] = {}
    for node in tree.body:
        dump = ast.dump(node, include_attributes=False)
        out.setdefault(_node_key(node, dump), []).append(dump)
    return out


def _added(base_dumps: List[str], other_dumps: List[str]) -> List[str]:
    """Nodes of ``other`` that are not in ``base`` (multiset difference, order kept)."""
    pool = list(base_dumps)
    out: List[str] = []
    for dump in other_dumps:
        if dump in pool:
            pool.remove(dump)
        else:
            out.append(dump)
    return out


def _present(needed: List[str], haystack: List[str]) -> List[str]:
    """The needed dumps that are missing from the haystack (multiset)."""
    pool = list(haystack)
    missing: List[str] = []
    for dump in needed:
        if dump in pool:
            pool.remove(dump)
        else:
            missing.append(dump)
    return missing


def merge_python(path: str, base: str, ours: str, theirs: str, *, protected: Sequence[ProtectedRegion] = ()) -> MergeResult:
    """``merge3`` plus the structural proof over the top-level statements, matched by identity (a definition by
    its name, an assignment by its targets, anything else by its text): a statement added by a side is present; a
    statement modified by exactly one side equals that side's version; one both sides modified is at least not the
    base version (the textual merge decided its content and the copy's verification judges it); an untouched one is
    unchanged. A merge that fails any of these is a conflict even when textually clean."""
    result = merge3(base, ours, theirs, protected=protected, path=path)
    if result.text is None:
        return result
    merged_keyed = _keyed(result.text)
    if merged_keyed is None:
        result.conflicts.append(Conflict(kind="lost_node", base_start=0, base_end=0, detail="the merged text does not parse"))
        result.text = None
        return result
    base_keyed, ours_keyed, theirs_keyed = _keyed(base), _keyed(ours), _keyed(theirs)
    if base_keyed is None or ours_keyed is None or theirs_keyed is None:
        result.evidence["structural_proof"] = "skipped: a side does not parse as Python"
        return result
    problems: List[str] = []
    both_modified: List[str] = []
    duplicates = 0
    added = {"ours": 0, "theirs": 0}
    for key in sorted(set(base_keyed) | set(ours_keyed) | set(theirs_keyed)):
        b, o, t, m = (sorted(d.get(key, [])) for d in (base_keyed, ours_keyed, theirs_keyed, merged_keyed))
        label = f"{key[0]} {key[1][:60]}"
        if not b:
            if o:
                added["ours"] += 1
            if t:
                added["theirs"] += 1
            needed = o if o == t else o + t
            if _present(needed, m):
                problems.append(f"{label}: added by a side, missing from the merge")
            if o and t and len(m) > 1:
                duplicates += 1
            continue
        if o == b and t == b:
            if m != b:
                problems.append(f"{label}: untouched by both sides, altered by the merge")
        elif t == b:
            if m != o:
                problems.append(f"{label}: changed by ours, the merge does not hold ours' version")
        elif o == b:
            if m != t:
                problems.append(f"{label}: changed by theirs, the merge does not hold theirs' version")
        elif o == t:
            if m != o:
                problems.append(f"{label}: changed identically by both, the merge differs")
        else:
            both_modified.append(label)
            if m == b:
                problems.append(f"{label}: changed by both sides, the merge kept the base version")
    if problems:
        result.conflicts.append(Conflict(kind="lost_node", base_start=0, base_end=0, detail="; ".join(problems[:5])))
        result.text = None
        return result
    result.evidence["structural_proof"] = {"added_by_ours": added["ours"], "added_by_theirs": added["theirs"], "all_present": True, "both_modified": both_modified}
    if duplicates:
        result.evidence["duplicate_additions"] = duplicates
    return result


def _merge_values(base: Any, ours: Any, theirs: Any, path: str, conflicts: List[Conflict], counts: Dict[str, int]) -> Any:
    if ours == theirs:
        if ours != base:
            counts["identical"] += 1
        return ours
    if theirs == base:
        counts["ours"] += 1
        return ours
    if ours == base:
        counts["theirs"] += 1
        return theirs
    if isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict):
        out: Dict[str, Any] = {}
        for key in sorted(set(base) | set(ours) | set(theirs)):
            sentinel = object()
            b, o, t = base.get(key, sentinel), ours.get(key, sentinel), theirs.get(key, sentinel)
            if o is sentinel and t is sentinel:
                continue  # removed by both
            if b is sentinel:  # added by one side, or by both
                if o is sentinel:
                    counts["theirs"] += 1
                    out[key] = t
                elif t is sentinel:
                    counts["ours"] += 1
                    out[key] = o
                elif o == t:
                    counts["identical"] += 1
                    out[key] = o
                else:
                    out[key] = _merge_values(None, o, t, f"{path}/{key}", conflicts, counts) if isinstance(o, dict) and isinstance(t, dict) else base
                    if not (isinstance(o, dict) and isinstance(t, dict)):
                        conflicts.append(Conflict(kind="json_value", base_start=0, base_end=0, detail=f"{path}/{key}: added by both sides with different values"))
                continue
            if o is sentinel and t == b:
                counts["ours"] += 1
                continue  # removed by ours, untouched by theirs
            if t is sentinel and o == b:
                counts["theirs"] += 1
                continue
            if o is sentinel or t is sentinel:
                conflicts.append(Conflict(kind="json_value", base_start=0, base_end=0, detail=f"{path}/{key}: removed by one side and changed by the other"))
                continue
            out[key] = _merge_values(b if b is not sentinel else None, o, t, f"{path}/{key}", conflicts, counts)
        return out
    conflicts.append(Conflict(kind="json_value", base_start=0, base_end=0, detail=f"{path or '/'}: both sides changed the value differently"))
    return base


def merge_json(base: str, ours: str, theirs: str, *, indent: int = 2) -> MergeResult:
    """Structural three-way merge of JSON objects; ``text`` is the canonical dump (sorted keys) or None on conflict."""
    result = MergeResult(text=None)
    try:
        b, o, t = json.loads(base), json.loads(ours), json.loads(theirs)
    except ValueError as exc:
        result.conflicts.append(Conflict(kind="json_value", base_start=0, base_end=0, detail=f"a side is not valid JSON ({exc})"))
        return result
    counts = {"ours": 0, "theirs": 0, "identical": 0}
    merged = _merge_values(b, o, t, "", result.conflicts, counts)
    result.applied = counts
    result.evidence = {"base_sha256": _digest(base), "ours_sha256": _digest(ours), "theirs_sha256": _digest(theirs)}
    if result.conflicts:
        return result
    result.text = json.dumps(merged, indent=indent, sort_keys=True) + "\n"
    result.evidence["result_sha256"] = _digest(result.text)
    return result


__all__ = ["Conflict", "Hunk", "MergeResult", "hunks_between", "merge3", "merge_json", "merge_python"]
