"""Conservative project call graph over function summaries (v1.5, extended in v1.6).

Resolution is purely syntactic: a call site becomes an edge only when its
target is a function the project defines. v1.5 resolved imported and
module-level names, ``self.method``, instances of project classes and methods
through declared bases. v1.6 adds, each still without executing anything:

* instance attributes typed in ``__init__`` (``self._repo.get()``);
* results of project calls with an annotated return type (``repo = make_repo(); repo.get()``);
* functions handed as arguments (``run_with(slow, x)`` -> ``handler(x)``), resolved only
  when every caller hands in the same function; otherwise the site is ``ambiguous``;
* interfaces: a parameter typed with a Protocol or abstract class resolves to the
  single implementation when exactly one exists (nominal subclass or structural match),
  with ``inferred`` evidence; several implementations stay ``ambiguous``;
* class attributes and static/class methods through re-exports and relative imports.

Every call site is classified (``project``, ``external``, ``builtin``, ``object``,
``constructors``, ``ambiguous``, ``not_found``, ``dynamic``) and the counts are
reported, so the reader knows how much of the graph was resolved and how much
was uncertain. Limits (all reported): maximum path depth, a wall-clock budget,
bounded propagation iterations, a cap on implementations examined per
interface, and cycle protection through visited sets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from .kinds import SESSION_KINDS, TYPE_KINDS
from .summaries import CallSite, ClassRecord, FunctionRecord

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ProjectFacts

DEFAULT_MAX_CALL_DEPTH = 4
DEFAULT_TIME_BUDGET_S = 5.0
MAX_BASE_WALK = 4
DEFAULT_MAX_IMPLEMENTATIONS = 16  # implementations examined per interface before giving up (ambiguous)

EDGE_DIRECT = "direct"
EDGE_ATTRIBUTE = "attribute"
EDGE_RETURN = "return"
EDGE_CALLBACK = "callback"
EDGE_INTERFACE = "interface"


@dataclass
class CallGraphStats:
    enabled: bool = True
    max_depth: int = DEFAULT_MAX_CALL_DEPTH
    time_budget_s: float = DEFAULT_TIME_BUDGET_S
    functions: int = 0
    classes: int = 0
    call_sites: int = 0
    project_edges: int = 0
    external: int = 0
    dynamic: int = 0
    not_found: int = 0
    constructors: int = 0
    builtin: int = 0
    objects: int = 0
    ambiguous: int = 0
    edges_direct: int = 0
    edges_attribute: int = 0
    edges_return: int = 0
    edges_callback: int = 0
    edges_interface: int = 0
    callback_params_unresolved: int = 0
    interfaces: int = 0
    dependency_params: int = 0
    inferred_params: int = 0
    attribute_kinds: int = 0
    propagation_rounds: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    truncated_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        # elapsed_ms is deliberately not serialised: reports must be byte-identical across runs.
        resolvable = self.project_edges + self.not_found + self.ambiguous
        classified = self.call_sites - self.dynamic
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
                "builtin": self.builtin,  # schema 1.4
                "object": self.objects,  # schema 1.4
                "ambiguous": self.ambiguous,  # schema 1.4
            },
            "edges": {  # schema 1.4: how each project edge was resolved
                "direct": self.edges_direct,
                "attribute": self.edges_attribute,
                "return": self.edges_return,
                "callback": self.edges_callback,
                "interface": self.edges_interface,
            },
            "coverage": {  # schema 1.4: honest ratios, separated from the raw edge count
                "resolvable_sites": resolvable,
                "project_call_coverage": round(self.project_edges / resolvable, 4) if resolvable else None,
                "classified_ratio": round(classified / self.call_sites, 4) if self.call_sites else None,
            },
            "uncertainty": {  # schema 1.4
                "ambiguous_sites": self.ambiguous,
                "dynamic_sites": self.dynamic,
                "callback_params_unresolved": self.callback_params_unresolved,
                "interfaces": self.interfaces,
            },
            "limits": {  # schema 1.4
                "max_depth": self.max_depth,
                "time_budget_s": self.time_budget_s,
                "max_base_walk": MAX_BASE_WALK,
                "max_implementations": DEFAULT_MAX_IMPLEMENTATIONS,
                "propagation_rounds_max": self.max_depth + 1,
            },
            "dependency_params": self.dependency_params,
            "inferred_params": self.inferred_params,
            "attribute_kinds": self.attribute_kinds,  # schema 1.4
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
        self.site_candidates: Dict[Tuple[str, int, int], List[str]] = {}  # ambiguous sites -> candidate keys
        self.edge_kinds: Dict[Tuple[str, int, int], str] = {}  # how a project edge was resolved
        self.edge_evidence: Dict[Tuple[str, int, int], str] = {}  # verified | inferred
        self.stats = CallGraphStats(max_depth=self.max_depth, time_budget_s=time_budget_s)
        self._blocking_memo: Dict[str, Optional[Tuple[List[str], str, str]]] = {}
        self._impl_memo: Dict[str, List[str]] = {}
        self._attr_kind_memo: Dict[str, Dict[str, Tuple[str, str]]] = {}
        self._project_roots = {name.split(".")[0] for name in facts.module_map}
        self._start = 0.0

    # ------------------------------------------------------------------ resolution
    def _over_budget(self) -> bool:
        return time.monotonic() - self._start > self.time_budget_s

    def _method_through_bases(self, cls_key: str, method: str, depth: int = 0) -> Optional[str]:
        record = self.class_record(cls_key)
        if record is None or depth > MAX_BASE_WALK:
            return None
        if method in record.methods:
            return record.methods[method]
        for base in record.bases:
            found = self._method_through_bases(base, method, depth + 1)
            if found:
                return found
        return None

    def class_record(self, dotted: Optional[str]) -> Optional[ClassRecord]:
        key = self.canonical_class(dotted)
        return self.facts.classes.get(key) if key else None

    def canonical_function(self, dotted: Optional[str]) -> Optional[str]:
        """Function key for a dotted name, accepting re-exports and source-root aliases (app.x.f == src.app.x.f)."""
        if not dotted:
            return None
        for candidate in (dotted, self.facts.canonical(dotted)):
            if not candidate:
                continue
            if candidate in self.functions:
                return candidate
            alias = self.facts.function_aliases.get(candidate)
            if alias:
                return alias
        return None

    def canonical_class(self, dotted: Optional[str]) -> Optional[str]:
        if not dotted:
            return None
        for candidate in (dotted, self.facts.canonical(dotted)):
            if not candidate:
                continue
            if candidate in self.facts.classes:
                return candidate
            alias = self.facts.class_aliases.get(candidate)
            if alias:
                return alias
        return None

    def implementations(self, cls_key: str) -> List[str]:
        """Project classes implementing an interface: nominal subclasses, plus structural matches
        for Protocols (every protocol method defined). Bounded by DEFAULT_MAX_IMPLEMENTATIONS."""
        if cls_key in self._impl_memo:
            return self._impl_memo[cls_key]
        iface = self.facts.classes.get(cls_key)
        found: List[str] = []
        if iface is not None and (iface.is_protocol or iface.is_abstract):
            required = set(iface.methods) - {"__init__"}
            for key in sorted(self.facts.classes):
                if len(found) > DEFAULT_MAX_IMPLEMENTATIONS:
                    break
                if key == cls_key:
                    continue
                cand = self.facts.classes[key]
                if cand.is_protocol:
                    continue
                nominal = any(self.canonical_class(b) == cls_key for b in cand.bases)
                structural = bool(required) and iface.is_protocol and all(self._method_through_bases(key, m) for m in required)
                if nominal or structural:
                    found.append(key)
        self._impl_memo[cls_key] = found
        return found

    def _class_site(self, cls_key: str, method: str, verified: bool) -> Tuple[str, Optional[str], str, str, List[str]]:
        """Resolve ``<instance of cls_key>.method()``: (status, key, edge_kind_hint, evidence, candidates)."""
        record = self.facts.classes.get(cls_key)
        found = self._method_through_bases(cls_key, method)
        if record is not None and (record.is_protocol or (record.is_abstract and method in record.abstract_methods)) and (found is None or found.split(".")[-2] == cls_key.split(".")[-1]):
            impls = self.implementations(cls_key)
            candidates = sorted({m for m in (self._method_through_bases(i, method) for i in impls) if m})
            if len(candidates) == 1:
                return "project", candidates[0], EDGE_INTERFACE, "inferred", candidates
            if len(candidates) > 1:
                return "ambiguous", None, EDGE_INTERFACE, "unknown", candidates
            return "dynamic", None, EDGE_INTERFACE, "unknown", []
        if found:
            return "project", found, EDGE_DIRECT, "verified" if verified else "inferred", [found]
        attr = record.attrs.get(method) if record is not None else None
        if attr is not None:  # self._app(scope, receive, send): a callable stored in an attribute, not a method
            if attr.type and (attr.type.startswith("builtins.") or self.canonical_class(attr.type) is None and attr.type.split(".")[0] not in self._project_roots):
                return "external", None, EDGE_DIRECT, "verified", []
            return "dynamic", None, EDGE_DIRECT, "unknown", []
        return "not_found", None, EDGE_DIRECT, "unknown", []

    def resolve_site(self, site: CallSite, record: Optional[FunctionRecord] = None) -> Tuple[str, Optional[str]]:
        status, key, _kind, _ev, _cands = self._resolve(site, record)
        return status, key

    def _resolve(self, site: CallSite, record: Optional[FunctionRecord]) -> Tuple[str, Optional[str], str, str, List[str]]:
        if site.status == "builtin":
            return "builtin", None, EDGE_DIRECT, "verified", []
        if site.status == "external":
            return "external", None, EDGE_DIRECT, "verified", []
        if site.status == "object":
            return "object", None, EDGE_DIRECT, "verified", []
        if site.status == "hint" and site.hint:
            kind = site.hint[0]
            if kind == "attr" and record is not None and record.class_name:
                cls = self.class_record_for(record.file, record.class_name)
                info = cls.attrs.get(site.hint[1]) if cls is not None else None
                if info is None:
                    return "dynamic", None, EDGE_ATTRIBUTE, "unknown", []
                if info.type and info.type.startswith("builtins."):
                    return "builtin", None, EDGE_ATTRIBUTE, "verified", []
                target_cls = self.canonical_class(info.type) if info.type else None
                if target_cls:
                    status, key, _k, ev, cands = self._class_site(target_cls, site.hint[2], verified=info.source != "init-other")
                    return status, key, EDGE_ATTRIBUTE if _k == EDGE_DIRECT else _k, ev, cands
                if info.kind or info.factory or (info.type and info.type.split(".")[0] not in self._project_roots and (self.facts.canonical(info.type) or info.type).split(".")[0] not in self._project_roots):
                    return "external", None, EDGE_ATTRIBUTE, "verified", []
                return "dynamic", None, EDGE_ATTRIBUTE, "unknown", []
            if kind == "ret":
                callee = self.functions.get(self.canonical_function(site.hint[1]) or "")
                if callee is None:
                    return "dynamic", None, EDGE_RETURN, "unknown", []
                if callee.returns_class:
                    target_cls = self.canonical_class(callee.returns_class)
                    if target_cls:
                        status, key, _k, ev, cands = self._class_site(target_cls, site.hint[2], verified=True)
                        return status, key, EDGE_RETURN if _k == EDGE_DIRECT else _k, ev, cands
                if callee.returns_kind:
                    return "external", None, EDGE_RETURN, "verified", []
                return "dynamic", None, EDGE_RETURN, "unknown", []
            if kind == "callback" and record is not None:
                candidates = sorted(record.callable_params.get(site.hint[1], set()))
                if len(candidates) == 1:
                    return "project", candidates[0], EDGE_CALLBACK, "inferred", candidates
                if len(candidates) > 1:
                    return "ambiguous", None, EDGE_CALLBACK, "unknown", candidates
                return "dynamic", None, EDGE_CALLBACK, "unknown", []
            return "dynamic", None, EDGE_DIRECT, "unknown", []
        if site.status != "candidate" or not site.target:
            return "dynamic", None, EDGE_DIRECT, "unknown", []
        target = site.target
        key = self.canonical_function(target)
        if key:
            return "project", key, EDGE_DIRECT, "verified", [key]
        cls_key, _, method = target.rpartition(".")
        cls_canon = self.canonical_class(cls_key) if cls_key else None
        if cls_canon:
            status, key, k, ev, cands = self._class_site(cls_canon, method, verified=True)
            if status != "not_found":
                return status, key, k, ev, cands
        target_cls = self.canonical_class(target)
        if target_cls:
            init = f"{target_cls}.__init__"
            if init in self.functions:
                return "project", init, EDGE_DIRECT, "verified", [init]
            return "constructor", None, EDGE_DIRECT, "verified", []
        canon = self.facts.canonical(target) or target
        for dotted in (target, canon):
            owner, _, attr_name = dotted.rpartition(".")
            instance_cls = self.facts.module_instances.get(owner) if owner else None
            if instance_cls and self.canonical_class(instance_cls):  # module-level instance of a project class: dispatcher.publish()
                status, key, k, ev, cands = self._class_site(self.canonical_class(instance_cls), attr_name, verified=True)
                if status != "not_found":
                    return status, key, EDGE_ATTRIBUTE if k == EDGE_DIRECT else k, ev, cands
            if dotted in self.facts.module_objects:
                return "object", None, EDGE_DIRECT, "verified", []
            if owner and owner in self.facts.module_objects:
                return "object", None, EDGE_DIRECT, "verified", []  # router.get(...), context_var.get(...)
        root = canon.split(".")[0]
        if root in self._project_roots or target.split(".")[0] in self._project_roots:
            # the name lives under a project root: only "not found" when its module really is a
            # project file; a library sharing a directory name (alembic/, tests/) is external
            if self.facts.resolve_import(canon) or self.facts.resolve_import(target):
                return "not_found", None, EDGE_DIRECT, "unknown", []
        return "external", None, EDGE_DIRECT, "verified", []

    # ------------------------------------------------------------------ build
    def _record_edge(self, caller_key: str, site: CallSite, callee: str, edge_kind: str, evidence: str) -> None:
        record = self.functions[caller_key]
        loc = (record.file, site.lineno, site.col)
        self.site_targets[loc] = ("project", callee)
        self.edge_kinds[loc] = edge_kind
        self.edge_evidence[loc] = evidence
        self.edges.setdefault(caller_key, []).append((site, callee))
        self.callers.setdefault(callee, set()).add(caller_key)
        self.stats.project_edges += 1
        counter = {EDGE_DIRECT: "edges_direct", EDGE_ATTRIBUTE: "edges_attribute", EDGE_RETURN: "edges_return", EDGE_CALLBACK: "edges_callback", EDGE_INTERFACE: "edges_interface"}[edge_kind]
        setattr(self.stats, counter, getattr(self.stats, counter) + 1)

    def _count_status(self, status: str) -> None:
        field = {"external": "external", "not_found": "not_found", "constructor": "constructors", "builtin": "builtin", "object": "objects", "ambiguous": "ambiguous"}.get(status, "dynamic")
        setattr(self.stats, field, getattr(self.stats, field) + 1)

    def build(self) -> "CallGraph":
        self._start = time.monotonic()
        self.stats.functions = len(self.functions)
        self.stats.classes = len(self.facts.classes)
        self.stats.interfaces = sum(1 for c in self.facts.classes.values() if c.is_protocol or c.is_abstract)
        deferred: List[Tuple[str, CallSite]] = []  # callback hints wait for callable propagation
        for key in sorted(self.functions):
            if self._over_budget():
                self.stats.truncated = True
                self.stats.truncated_reason = f"time budget of {self.time_budget_s:.1f}s exceeded while building edges"
                break
            record = self.functions[key]
            for site in record.calls:
                self.stats.call_sites += 1
                if site.status == "hint" and site.hint and site.hint[0] == "callback":
                    deferred.append((key, site))
                    continue
                status, callee, edge_kind, evidence, candidates = self._resolve(site, record)
                loc = (record.file, site.lineno, site.col)
                if status == "project" and callee:
                    self._record_edge(key, site, callee, edge_kind, evidence)
                else:
                    self.site_targets[loc] = (status, None)
                    if status == "ambiguous":
                        self.site_candidates[loc] = candidates
                    self._count_status(status)
        self._propagate()
        # second pass: callbacks, once callers' arguments have flowed into callable_params
        for key, site in deferred:
            record = self.functions[key]
            status, callee, edge_kind, evidence, candidates = self._resolve(site, record)
            loc = (record.file, site.lineno, site.col)
            if status == "project" and callee:
                self._record_edge(key, site, callee, edge_kind, evidence)
            else:
                self.site_targets[loc] = (status, None)
                if status == "ambiguous":
                    self.site_candidates[loc] = candidates
                else:
                    self.stats.callback_params_unresolved += 1
                self._count_status(status)
        if deferred and not self.stats.truncated:
            self._propagate_kinds(extra_round=True)  # a callback may receive a session
        self.stats.inferred_params = sum(len(r.inferred_kinds) for r in self.functions.values())
        self.stats.attribute_kinds = sum(1 for c in self.facts.classes.values() for a in c.attrs.values() if a.kind)
        self.stats.elapsed_ms = int((time.monotonic() - self._start) * 1000)
        return self

    # ------------------------------------------------------------------ kinds
    def _propagate(self) -> None:
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
        self._propagate_kinds()

    def _propagate_kinds(self, extra_round: bool = False) -> None:
        # 2. caller -> callee argument flow (kinds and callables), bounded fixpoint
        rounds = range(1, 2) if extra_round else range(1, self.max_depth + 2)
        for round_no in rounds:
            if self._over_budget():
                self.stats.truncated = True
                self.stats.truncated_reason = f"time budget of {self.time_budget_s:.1f}s exceeded during kind propagation"
                break
            changed = False
            for caller_key in sorted(self.edges):
                caller = self.functions[caller_key]
                for site, callee_key in self.edges[caller_key]:
                    callee = self.functions[callee_key]
                    offset = 1 if (callee.class_name and callee.params and callee.params[0] in {"self", "cls"} and (site.has_receiver or self.edge_kinds.get((caller.file, site.lineno, site.col)) == EDGE_CALLBACK)) else 0

                    def param_for(position):
                        if isinstance(position, int):
                            index = position + offset
                            return callee.params[index] if index < len(callee.params) else None
                        return position if position in callee.params else None

                    for position, name in site.arg_names:
                        kind, _ = caller.kind_of(name)
                        if not kind:
                            continue
                        param = param_for(position)
                        if param is None or param in callee.declared_kinds or param in callee.dependency_kinds:
                            continue
                        if callee.inferred_kinds.get(param) != kind:
                            callee.inferred_kinds[param] = kind
                            changed = True
                    for position, dotted in site.arg_callables:
                        fn_key = self.canonical_function(dotted)
                        if fn_key is None:
                            continue
                        param = param_for(position)
                        if param is None:
                            continue
                        bucket = callee.callable_params.setdefault(param, set())
                        if fn_key not in bucket:
                            bucket.add(fn_key)
                            changed = True
                    # a parameter that only forwards its callable to another helper: flow it on
                    for position, name in site.arg_names:
                        handed = caller.callable_params.get(name)
                        if not handed:
                            continue
                        param = param_for(position)
                        if param is None:
                            continue
                        bucket = callee.callable_params.setdefault(param, set())
                        if not handed <= bucket:
                            bucket |= handed
                            changed = True
            if not extra_round:
                self.stats.propagation_rounds = round_no
            if not changed:
                break

    # ------------------------------------------------------------------ queries
    def record_for(self, rel: str, qualname: str) -> Optional[FunctionRecord]:
        module = self.facts.file_module.get(rel, "")
        key = f"{module}.{qualname}" if module else qualname
        return self.functions.get(key)

    def class_record_for(self, rel: str, class_name: str) -> Optional[ClassRecord]:
        module = self.facts.file_module.get(rel, "")
        key = f"{module}.{class_name}" if module else class_name
        return self.facts.classes.get(key)

    def attribute_kinds(self, rel: str, class_name: str) -> Dict[str, Tuple[str, str]]:
        """``{attr: (kind, evidence)}`` for ``self.<attr>`` of a class: kinds declared in ``__init__``
        are ``verified``; kinds that reached an untyped ``__init__`` parameter through callers are ``inferred``."""
        module = self.facts.file_module.get(rel, "")
        key = f"{module}.{class_name}" if module else class_name
        if key in self._attr_kind_memo:
            return self._attr_kind_memo[key]
        out: Dict[str, Tuple[str, str]] = {}
        cls = self.facts.classes.get(key)
        if cls is not None:
            init = self.functions.get(f"{key}.__init__")
            for attr, info in cls.attrs.items():
                if info.kind:
                    out[attr] = (info.kind, "verified")
                elif info.init_param and init is not None:
                    kind, ev = init.kind_of(info.init_param)
                    if kind and ev:
                        out[attr] = (kind, ev)
        self._attr_kind_memo[key] = out
        return out

    def edge_info(self, rel: str, lineno: int, col: int) -> Tuple[str, str]:
        """(edge kind, evidence) of a resolved project edge at a call site."""
        loc = (rel, lineno, col)
        return self.edge_kinds.get(loc, EDGE_DIRECT), self.edge_evidence.get(loc, "verified")

    def blocking_path(self, key: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[List[str], str, str]]:
        """Shortest known path of synchronous project calls from ``key`` to a blocking call:
        ``(chain, symbol, evidence)`` where evidence is ``inferred`` when any edge on the chain is."""
        if key in self._blocking_memo:
            return self._blocking_memo[key]
        record = self.functions.get(key)
        if record is None:
            return None
        if record.blocking_direct:
            result: Optional[Tuple[List[str], str, str]] = ([key], record.blocking_direct[0][1], "verified")
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
                _k, ev = self.edge_info(record.file, site.lineno, site.col)
                evidence = "inferred" if (ev == "inferred" or sub[2] == "inferred") else "verified"
                result = ([key, *sub[0]], sub[1], evidence)
                self._blocking_memo[key] = result
                return result
        self._blocking_memo[key] = None
        return None

    def site_target(self, rel: str, lineno: int, col: int) -> Tuple[str, Optional[str]]:
        return self.site_targets.get((rel, lineno, col), ("dynamic", None))
