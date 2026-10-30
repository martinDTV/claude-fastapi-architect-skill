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

v1.8 (precision): locals bound to external/builtin call results, unpacking and closures
are classified instead of left dynamic; ASGI callbacks (``send``/``receive``) are external;
typed ``Callable`` parameters, lambdas forwarding to one target and bound method references
feed callback resolution; ``self.a.b.method()`` follows two typed attributes; a method a base
class leaves to its subclasses resolves to the unique implementation (``inferred``) or stays
``ambiguous``; unknown methods of classes with a third-party base are external; mixins borrow
attribute kinds from their concrete subclasses when every subclass agrees. Every still-dynamic
site carries an origin (loop target, comprehension, untyped parameter, ...) so the uncertainty
is itemised rather than hidden.

Every call site is classified (``project``, ``external``, ``builtin``, ``object``,
``constructors``, ``ambiguous``, ``not_found``, ``dynamic``) and the counts are
reported, so the reader knows how much of the graph was resolved and how much
was uncertain. Limits (all reported): maximum path depth, a wall-clock budget,
bounded propagation iterations, a cap on implementations examined per
interface, and cycle protection through visited sets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
STRUCTURAL_BASES = frozenset({"object", "builtins.object", "typing.Protocol", "typing_extensions.Protocol", "typing.Generic", "abc.ABC", "abc.ABCMeta"})


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
    # v1.8
    framework_callbacks: int = 0  # ASGI send/receive/app calls classified external
    callback_sites_unresolved: int = 0  # call sites through a callback parameter with no known provider
    mixin_attribute_kinds: int = 0  # attribute kinds a mixin borrowed from its concrete subclasses
    subclass_hooks: int = 0  # self.method() resolved to the unique subclass implementation
    external_base_methods: int = 0  # self.method() attributed to a third-party base class
    dynamic_by_origin: Dict[str, int] = field(default_factory=dict)
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
                # schema 1.6: the three shares of every call site, so a high classified_ratio cannot hide a low project share
                "project_site_ratio": round(self.project_edges / self.call_sites, 4) if self.call_sites else None,
                "external_site_ratio": round((self.external + self.builtin + self.objects + self.constructors) / self.call_sites, 4) if self.call_sites else None,
                "unresolved_site_ratio": round((self.dynamic + self.not_found + self.ambiguous) / self.call_sites, 4) if self.call_sites else None,
            },
            "uncertainty": {  # schema 1.4
                "ambiguous_sites": self.ambiguous,
                "dynamic_sites": self.dynamic,
                "callback_params_unresolved": self.callback_params_unresolved,  # schema 1.6: distinct parameters (was call sites, and counted ASGI callbacks)
                "interfaces": self.interfaces,
                "callback_sites_unresolved": self.callback_sites_unresolved,  # schema 1.6
                "framework_callbacks": self.framework_callbacks,  # schema 1.6
                "dynamic_by_origin": dict(sorted(self.dynamic_by_origin.items())),  # schema 1.6
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
            "mixin_attribute_kinds": self.mixin_attribute_kinds,  # schema 1.6
            "subclass_hooks": self.subclass_hooks,  # schema 1.6
            "external_base_methods": self.external_base_methods,  # schema 1.6
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
        self._children: Optional[Dict[str, List[str]]] = None  # v1.8: class -> direct project subclasses
        self._mixin_memo: Dict[str, Dict[str, str]] = {}
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

    # ------------------------------------------------------------------ v1.8 class hierarchy helpers
    def _subclass_index(self) -> Dict[str, List[str]]:
        if self._children is None:
            children: Dict[str, List[str]] = {}
            for key in sorted(self.facts.classes):
                for base in self.facts.classes[key].bases:
                    canon = self.canonical_class(base)
                    if canon and canon != key:
                        children.setdefault(canon, []).append(key)
            self._children = children
        return self._children

    def subclasses(self, cls_key: str, depth: int = 0, seen: Optional[Set[str]] = None) -> List[str]:
        """Project classes deriving from ``cls_key`` (transitively, bounded by MAX_BASE_WALK), sorted."""
        seen = seen if seen is not None else set()
        if depth > MAX_BASE_WALK:
            return []
        out: List[str] = []
        for child in self._subclass_index().get(cls_key, []):
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            out.extend(self.subclasses(child, depth + 1, seen))
        return sorted(set(out))

    def _subclass_methods(self, cls_key: str, method: str) -> List[str]:
        """Implementations of ``method`` defined by subclasses of ``cls_key`` (a hook the base leaves open)."""
        found: Set[str] = set()
        for sub in self.subclasses(cls_key):
            record = self.facts.classes.get(sub)
            if record is not None and method in record.methods:
                found.add(record.methods[method])
        return sorted(found)

    def _has_external_base(self, cls_key: str, depth: int = 0) -> bool:
        """True when the class derives (within MAX_BASE_WALK) from a third-party class: unknown methods belong to it."""
        record = self.facts.classes.get(cls_key)
        if record is None or depth > MAX_BASE_WALK:
            return False
        for base in record.bases:
            canon = self.canonical_class(base)
            if canon:
                if self._has_external_base(canon, depth + 1):
                    return True
                continue
            dotted = self.facts.canonical(base) or base
            root = dotted.split(".")[0]
            if dotted in STRUCTURAL_BASES or dotted.rsplit(".", 1)[-1] in {"Protocol", "Generic", "ABC", "ABCMeta"}:
                continue  # Protocol / Generic / ABC / object carry no methods of their own
            if root in self._project_roots and self.facts.resolve_import(dotted):
                continue  # a project class we failed to index: not external
            return True  # third-party, stdlib (TypedDict, NamedTuple, Enum, UserDict) or builtin (dict, Exception) base: its methods live there
        return False

    def _own_attr_kinds(self, cls_key: str) -> Dict[str, Optional[str]]:
        """``{attr: kind-or-None}`` for attributes a class sets itself (``__init__``/class annotations)."""
        cls = self.facts.classes.get(cls_key)
        out: Dict[str, Optional[str]] = {}
        if cls is None:
            return out
        init = self.functions.get(f"{cls_key}.__init__")
        for attr, info in cls.attrs.items():
            kind: Optional[str] = info.kind
            if kind is None and info.init_param and init is not None:
                kind = init.kind_of(info.init_param)[0]
            out[attr] = kind
        return out

    def mixin_attribute_kinds(self, cls_key: str) -> Dict[str, str]:
        """Kinds of ``self.<attr>`` a class never sets itself but every concrete subclass sets to the same kind
        (``SessionMixin`` reading ``self._session`` that ``ItemRepo.__init__`` typed as AsyncSession). Any subclass
        that sets the attribute to an unknown or different kind cancels the inference: ambiguity never becomes a kind."""
        if cls_key in self._mixin_memo:
            return self._mixin_memo[cls_key]
        cls = self.facts.classes.get(cls_key)
        agreed: Dict[str, str] = {}
        if cls is not None:
            seen: Dict[str, Set[Optional[str]]] = {}
            for sub in self.subclasses(cls_key):
                for attr, kind in self._own_attr_kinds(sub).items():
                    if attr in cls.attrs:
                        continue  # the class sets it itself: its own evidence wins, even when untyped
                    seen.setdefault(attr, set()).add(kind)
            for attr, kinds in seen.items():
                if len(kinds) == 1 and None not in kinds:
                    agreed[attr] = next(iter(kinds))  # type: ignore[arg-type]
        self._mixin_memo[cls_key] = agreed
        return agreed

    def _attr_site(self, cls_key: Optional[str], attr: str, method: str, verified: bool, edge_kind: str = EDGE_ATTRIBUTE) -> Tuple[str, Optional[str], str, str, List[str]]:
        """Resolve ``<instance of cls_key>.<attr>.<method>()`` through the attribute's declared type."""
        cls = self.facts.classes.get(cls_key) if cls_key else None
        info = cls.attrs.get(attr) if cls is not None else None
        if info is None:
            if cls_key and attr in self.mixin_attribute_kinds(cls_key):
                return "external", None, edge_kind, "inferred", []  # a session/client kind borrowed from the subclasses
            return "dynamic", None, edge_kind, "unknown", []
        if info.type and info.type.startswith("builtins."):
            return "builtin", None, edge_kind, "verified", []
        target_cls = self.canonical_class(info.type) if info.type else None
        if target_cls:
            status, key, k, ev, cands = self._class_site(target_cls, method, verified=verified and info.source != "init-other")
            return status, key, edge_kind if k == EDGE_DIRECT else k, ev, cands
        if info.kind or info.factory or (info.type and info.type.split(".")[0] not in self._project_roots and (self.facts.canonical(info.type) or info.type).split(".")[0] not in self._project_roots):
            return "external", None, edge_kind, "verified", []
        return "dynamic", None, edge_kind, "unknown", []

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
        if record is not None:
            # v1.8: a hook the class leaves to its subclasses (Template Method): one implementation resolves
            # with inferred evidence, several stay ambiguous; otherwise an unknown method of a class with a
            # third-party base belongs to that base (pydantic BaseModel.model_dump_json, Exception.add_note).
            hooks = self._subclass_methods(cls_key, method)
            if len(hooks) == 1:
                self.stats.subclass_hooks += 1
                return "project", hooks[0], EDGE_INTERFACE, "inferred", hooks
            if len(hooks) > 1:
                return "ambiguous", None, EDGE_INTERFACE, "unknown", hooks
            if self._has_external_base(cls_key):
                return "external_base", None, EDGE_DIRECT, "verified", []
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
            owner = (getattr(record, "owner_class", None) or record.class_name) if record is not None else None
            if kind == "attr" and owner:
                cls = self.class_record_for(record.file, owner)
                return self._attr_site(cls.key if cls is not None else None, site.hint[1], site.hint[2], verified=True)
            if kind == "attr2" and owner and len(site.hint) == 4:  # self.a.b.method()
                cls = self.class_record_for(record.file, owner)
                info = cls.attrs.get(site.hint[1]) if cls is not None else None
                if info is None or not info.type:
                    return "dynamic", None, EDGE_ATTRIBUTE, "unknown", []
                mid_cls = self.canonical_class(info.type)
                if mid_cls is None:
                    if info.type.startswith("builtins."):
                        return "builtin", None, EDGE_ATTRIBUTE, "verified", []
                    if (self.facts.canonical(info.type) or info.type).split(".")[0] not in self._project_roots:
                        return "external", None, EDGE_ATTRIBUTE, "verified", []
                    return "dynamic", None, EDGE_ATTRIBUTE, "unknown", []
                return self._attr_site(mid_cls, site.hint[2], site.hint[3], verified=info.source != "init-other")
            if kind == "iattr" and len(site.hint) == 4:  # obj.attr.method() where obj is a typed project instance
                return self._attr_site(self.canonical_class(site.hint[1]), site.hint[2], site.hint[3], verified=True)
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
                if callee.returns_builtin:  # -> str / -> list[dict]: methods of a builtin value
                    return "builtin", None, EDGE_RETURN, "verified", []
                if callee.returns_dotted:
                    root = callee.returns_dotted.split(".")[0]
                    if root not in self._project_roots and root not in {"typing", "typing_extensions", "builtins", "object"} and callee.returns_dotted != "object":
                        return "external", None, EDGE_RETURN, "verified", []  # -> httpx.Response: a library value
                return "dynamic", None, EDGE_RETURN, "unknown", []
            if kind == "callback" and record is not None:
                candidates = sorted(record.callable_params.get(site.hint[1], set()))
                if len(candidates) == 1:
                    return "project", candidates[0], EDGE_CALLBACK, "inferred", candidates
                if len(candidates) > 1:
                    return "ambiguous", None, EDGE_CALLBACK, "unknown", candidates
                if len(site.hint) > 2 and site.hint[2] == "asgi":
                    return "framework_callback", None, EDGE_CALLBACK, "verified", []  # receive()/send(): the ASGI server's callables
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

    def _count_status(self, status: str, site: Optional[CallSite] = None) -> None:
        if status == "framework_callback":
            self.stats.framework_callbacks += 1
            status = "external"
        elif status == "external_base":
            self.stats.external_base_methods += 1
            status = "external"
        field_name = {"external": "external", "not_found": "not_found", "constructor": "constructors", "builtin": "builtin", "object": "objects", "ambiguous": "ambiguous"}.get(status, "dynamic")
        setattr(self.stats, field_name, getattr(self.stats, field_name) + 1)
        if field_name == "dynamic" and site is not None:
            origin = self._origin_of(site)
            self.stats.dynamic_by_origin[origin] = self.stats.dynamic_by_origin.get(origin, 0) + 1

    @staticmethod
    def _origin_of(site: CallSite) -> str:
        """Why a site stayed dynamic: what the summaries recorded, else the shape of the receiver."""
        if site.origin:
            return site.origin
        if site.status == "hint" and site.hint:
            return {"attr": "attribute_untyped", "attr2": "attribute_chain", "iattr": "attribute_chain", "ret": "return_untyped", "callback": "callback_unresolved"}.get(site.hint[0], "hint")
        if site.status == "candidate":
            return "unresolved_name"
        return "expression"  # calls on call results, subscripts, operators: no name to type

    @staticmethod
    def _public_status(status: str) -> str:
        return "external" if status in {"framework_callback", "external_base"} else status

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
                    self.site_targets[loc] = (self._public_status(status), None)
                    if status == "ambiguous":
                        self.site_candidates[loc] = candidates
                    self._count_status(status, site)
        self._propagate()
        # second pass: callbacks, once callers' arguments have flowed into callable_params
        unresolved_params: Set[Tuple[str, str]] = set()
        for key, site in deferred:
            record = self.functions[key]
            status, callee, edge_kind, evidence, candidates = self._resolve(site, record)
            loc = (record.file, site.lineno, site.col)
            if status == "project" and callee:
                self._record_edge(key, site, callee, edge_kind, evidence)
            else:
                self.site_targets[loc] = (self._public_status(status), None)
                if status == "ambiguous":
                    self.site_candidates[loc] = candidates
                elif status == "dynamic":
                    self.stats.callback_sites_unresolved += 1
                    unresolved_params.add((key, site.hint[1] if site.hint else ""))
                self._count_status(status, site)
        self.stats.callback_params_unresolved = len(unresolved_params)
        if deferred and not self.stats.truncated:
            self._propagate_kinds(extra_round=True)  # a callback may receive a session
        self.stats.inferred_params = sum(len(r.inferred_kinds) for r in self.functions.values())
        self.stats.attribute_kinds = sum(1 for c in self.facts.classes.values() for a in c.attrs.values() if a.kind)
        self.stats.mixin_attribute_kinds = sum(len(self.mixin_attribute_kinds(k)) for k in sorted(self.facts.classes))
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
            for attr, kind in self.mixin_attribute_kinds(key).items():  # v1.8: borrowed from agreeing subclasses
                out.setdefault(attr, (kind, "inferred"))
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
