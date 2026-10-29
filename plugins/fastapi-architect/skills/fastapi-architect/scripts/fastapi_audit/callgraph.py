"""Conservative project call graph over function summaries (v1.5).

Resolution is purely syntactic: a call site becomes an edge only when its
target is a function the project defines (imported name, module-level name,
``self.method`` inside a class, ``instance.method`` where the instance was
built from a project class, or a method found through declared bases). Calls
through unknown objects stay ``dynamic``; targets outside the project are
``external``; targets under a project package that do not exist are
``not_found``. Every count is reported so the reader knows how much of the
graph was resolved.

Limits (all reported): maximum path depth, a wall-clock budget, bounded
propagation iterations, and cycle protection through visited sets. Nothing
is executed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from .summaries import CallSite, FunctionRecord

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ProjectFacts

DEFAULT_MAX_CALL_DEPTH = 4
DEFAULT_TIME_BUDGET_S = 5.0
MAX_BASE_WALK = 4


@dataclass
class CallGraphStats:
    enabled: bool = True
    max_depth: int = DEFAULT_MAX_CALL_DEPTH
    functions: int = 0
    classes: int = 0
    call_sites: int = 0
    project_edges: int = 0
    external: int = 0
    dynamic: int = 0
    not_found: int = 0
    constructors: int = 0
    dependency_params: int = 0
    inferred_params: int = 0
    propagation_rounds: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    truncated_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        # elapsed_ms is deliberately not serialised: reports must be byte-identical across runs.
        return {
            "enabled": self.enabled,
            "max_depth": self.max_depth,
            "functions": self.functions,
            "classes": self.classes,
            "call_sites": self.call_sites,
            "resolution": {
                "project": self.project_edges,
                "external": self.external,
                "dynamic": self.dynamic,
                "not_found": self.not_found,
                "constructors": self.constructors,
            },
            "dependency_params": self.dependency_params,
            "inferred_params": self.inferred_params,
            "propagation_rounds": self.propagation_rounds,
            "truncated": self.truncated,
            "truncated_reason": self.truncated_reason,
        }


class CallGraph:
    def __init__(self, facts: "ProjectFacts", *, max_depth: int = DEFAULT_MAX_CALL_DEPTH, time_budget_s: float = DEFAULT_TIME_BUDGET_S) -> None:
        self.facts = facts
        self.functions: Dict[str, FunctionRecord] = facts.functions
        self.max_depth = max(1, int(max_depth))
        self.time_budget_s = time_budget_s
        self.edges: Dict[str, List[Tuple[CallSite, str]]] = {}
        self.callers: Dict[str, Set[str]] = {}
        self.site_targets: Dict[Tuple[str, int, int], Tuple[str, Optional[str]]] = {}  # (file, line, col) -> (status, key)
        self.stats = CallGraphStats(max_depth=self.max_depth)
        self._blocking_memo: Dict[str, Optional[Tuple[List[str], str]]] = {}
        self._project_roots = {name.split(".")[0] for name in facts.module_map}

    # ------------------------------------------------------------------ resolution
    def _method_through_bases(self, cls_key: str, method: str, depth: int = 0) -> Optional[str]:
        record = self.facts.classes.get(cls_key) or self.facts.classes.get(self.facts.class_aliases.get(cls_key, ""))
        if record is None or depth > MAX_BASE_WALK:
            return None
        if method in record.methods:
            return record.methods[method]
        for base in record.bases:
            found = self._method_through_bases(base, method, depth + 1)
            if found:
                return found
        return None

    def canonical_function(self, dotted: Optional[str]) -> Optional[str]:
        """Function key for a dotted name, accepting source-root aliases (app.x.f == src.app.x.f)."""
        if not dotted:
            return None
        if dotted in self.functions:
            return dotted
        return self.facts.function_aliases.get(dotted)

    def canonical_class(self, dotted: Optional[str]) -> Optional[str]:
        if not dotted:
            return None
        if dotted in self.facts.classes:
            return dotted
        return self.facts.class_aliases.get(dotted)

    def resolve_site(self, site: CallSite) -> Tuple[str, Optional[str]]:
        if site.status != "candidate" or not site.target:
            return "dynamic", None
        target = site.target
        key = self.canonical_function(target)
        if key:
            return "project", key
        cls_key, _, method = target.rpartition(".")
        cls_canon = self.canonical_class(cls_key) if cls_key else None
        if cls_canon:
            found = self._method_through_bases(cls_canon, method)
            if found:
                return "project", found
        target_cls = self.canonical_class(target)
        if target_cls:
            init = f"{target_cls}.__init__"
            if init in self.functions:
                return "project", init
            return "constructor", None
        root = target.split(".")[0]
        if root in self._project_roots or target in self.facts.module_map:
            return "not_found", None
        return "external", None

    # ------------------------------------------------------------------ build
    def build(self) -> "CallGraph":
        start = time.monotonic()
        self.stats.functions = len(self.functions)
        self.stats.classes = len(self.facts.classes)
        for key in sorted(self.functions):
            if time.monotonic() - start > self.time_budget_s:
                self.stats.truncated = True
                self.stats.truncated_reason = f"time budget of {self.time_budget_s:.1f}s exceeded while building edges"
                break
            record = self.functions[key]
            for site in record.calls:
                self.stats.call_sites += 1
                status, callee = self.resolve_site(site)
                self.site_targets[(record.file, site.lineno, site.col)] = (status, callee)
                if status == "project" and callee:
                    self.stats.project_edges += 1
                    self.edges.setdefault(key, []).append((site, callee))
                    self.callers.setdefault(callee, set()).add(key)
                elif status == "external":
                    self.stats.external += 1
                elif status == "not_found":
                    self.stats.not_found += 1
                elif status == "constructor":
                    self.stats.constructors += 1
                else:
                    self.stats.dynamic += 1
        self._propagate(start)
        self.stats.elapsed_ms = int((time.monotonic() - start) * 1000)
        return self

    # ------------------------------------------------------------------ kinds
    def _propagate(self, start: float) -> None:
        # 1. dependencies: Depends(f) where f returns/yields a known kind
        for record in self.functions.values():
            for param, dotted in record.depends.items():
                if not dotted:
                    continue
                dep = self.functions.get(self.canonical_function(dotted) or "")
                if dep is None:
                    cls_key, _, method = dotted.rpartition(".")
                    cls_canon = self.canonical_class(cls_key) if cls_key else None
                    found = self._method_through_bases(cls_canon, method) if cls_canon else None
                    dep = self.functions.get(found) if found else None
                if dep and dep.returns_kind and param not in record.declared_kinds:
                    record.dependency_kinds[param] = dep.returns_kind
                    self.stats.dependency_params += 1
        # 2. caller -> callee argument flow, bounded fixpoint
        for rounds in range(1, self.max_depth + 2):
            if time.monotonic() - start > self.time_budget_s:
                self.stats.truncated = True
                self.stats.truncated_reason = f"time budget of {self.time_budget_s:.1f}s exceeded during kind propagation"
                break
            changed = False
            for caller_key in sorted(self.edges):
                caller = self.functions[caller_key]
                for site, callee_key in self.edges[caller_key]:
                    callee = self.functions[callee_key]
                    offset = 1 if (callee.class_name and callee.params and callee.params[0] in {"self", "cls"} and site.has_receiver) else 0
                    for position, name in site.arg_names:
                        kind, _ = caller.kind_of(name)
                        if not kind:
                            continue
                        if isinstance(position, int):
                            index = position + offset
                            if index >= len(callee.params):
                                continue
                            param = callee.params[index]
                        else:
                            param = position
                        if param in callee.declared_kinds or param in callee.dependency_kinds:
                            continue
                        if callee.inferred_kinds.get(param) != kind:
                            callee.inferred_kinds[param] = kind
                            changed = True
            self.stats.propagation_rounds = rounds
            if not changed:
                break
        self.stats.inferred_params = sum(len(r.inferred_kinds) for r in self.functions.values())

    # ------------------------------------------------------------------ queries
    def record_for(self, rel: str, qualname: str) -> Optional[FunctionRecord]:
        module = self.facts.file_module.get(rel, "")
        key = f"{module}.{qualname}" if module else qualname
        return self.functions.get(key)

    def blocking_path(self, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[List[str], str]]:
        """Shortest known path of synchronous project calls from ``key`` to a blocking call."""
        if key in self._blocking_memo:
            return self._blocking_memo[key]
        record = self.functions.get(key)
        if record is None:
            return None
        if record.blocking_direct:
            result: Optional[Tuple[List[str], str]] = ([key], record.blocking_direct[0][1])
            self._blocking_memo[key] = result
            return result
        # depth counts project functions in the chain after the caller: with max_depth=1 only the
        # direct callee's own calls are examined.
        if depth + 1 >= self.max_depth:
            return None  # not memoised: a shorter route may exist from another caller
        visited = (visited or set()) | {key}
        for site, callee_key in self.edges.get(key, []):
            callee = self.functions.get(callee_key)
            if callee is None or callee.is_async or callee_key in visited:
                continue
            sub = self.blocking_path(callee_key, depth + 1, visited)
            if sub:
                result = ([key, *sub[0]], sub[1])
                self._blocking_memo[key] = result
                return result
        self._blocking_memo[key] = None
        return None

    def site_target(self, rel: str, lineno: int, col: int) -> Tuple[str, Optional[str]]:
        return self.site_targets.get((rel, lineno, col), ("dynamic", None))
