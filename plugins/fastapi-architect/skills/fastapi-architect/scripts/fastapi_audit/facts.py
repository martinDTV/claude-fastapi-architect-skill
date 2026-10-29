"""Project-level facts collected in a first read-only pass.

Rules need a little knowledge beyond the file they inspect: where session
factories and ``Annotated`` session aliases are defined, which routers carry
dependencies, whether import-linter already governs boundaries, how files map
to layers and business modules, and the module-level import graph. Everything
here is inferred conservatively and reported in the ``facts`` block of the
report so the reader can see what was assumed.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .discovery import match_globs
from .kinds import FACTORY_KINDS, TYPE_KINDS
from .resolver import (
    Scope,
    annotation_to_dotted,
    collect_scope,
    dotted_name,
    iter_scope_nodes,
    module_imports,
    resolve_in_scopes,
)

CONFIG_FILENAME = ".audit-fastapi.json"
AUTH_RE = re.compile(
    r"auth|user|principal|token|perm|require|tenant|scope|security|oauth|jwt|api_?key|"
    r"current|identity|member|owner|admin|guard|policy|login|bearer|credential|role|rbac|acl",
    re.I,
)

# Ordered: the first matching layer wins. Globs match the posix relpath ("/"-prefixed too).
DEFAULT_LAYER_PATTERNS: List[Tuple[str, List[str]]] = [
    ("domain", ["*/domain/*", "*/entities/*", "*/value_objects/*", "*/ports.py", "*/ports/*"]),
    ("adapter", ["*/adapters/*", "*/infrastructure/*", "*/infra/*"]),
    ("repository", ["*/repository.py", "*/repositories.py", "*/repositories/*", "*/repo.py", "*/dao.py", "*/persistence/*"]),
    ("service", ["*/service.py", "*/services.py", "*/services/*", "*/use_cases/*", "*/usecases/*", "*/application/*"]),
    ("router", ["*/router.py", "*/routers.py", "*/routes.py", "*/endpoints.py", "*/routers/*", "*/routes/*", "*/endpoints/*", "*/api/*", "*/controllers/*", "*/presentation/*", "*/views.py"]),
    ("core", ["*/core/*", "*/shared/*", "*/common/*", "*/utils/*", "*/utils.py", "*/helpers.py"]),
    ("models", ["*/models.py", "*/models/*"]),
    ("schemas", ["*/schemas.py", "*/schemas/*"]),
]
MODULES_DIR_NAMES = ("modules", "features")


@dataclass
class ProjectFacts:
    root: str
    config: Dict[str, Any] = field(default_factory=dict)
    config_source: Optional[str] = None
    files: List[str] = field(default_factory=list)
    module_map: Dict[str, str] = field(default_factory=dict)  # dotted -> rel
    file_module: Dict[str, str] = field(default_factory=dict)  # rel -> dotted
    factories: Dict[str, str] = field(default_factory=dict)  # dotted -> produced kind
    factory_last: Dict[str, Set[str]] = field(default_factory=dict)  # last name -> kinds
    aliases: Dict[str, str] = field(default_factory=dict)  # dotted Annotated alias -> kind
    dependency_aliases: Dict[str, str] = field(default_factory=dict)  # dotted Annotated alias -> Depends target (dotted or bare name)
    protected_routers: Set[str] = field(default_factory=set)  # dotted router names
    global_auth_signal: bool = False
    global_auth_evidence: List[str] = field(default_factory=list)
    uses_fastapi: bool = False
    uses_sqlalchemy: bool = False
    uses_async_sqlalchemy: bool = False
    uses_alembic: bool = False
    import_linter: Optional[Dict[str, Any]] = None
    modules_root: Optional[str] = None  # posix dir relative to root
    modules_root_source: str = "none"  # config | inferred | none
    layers: Dict[str, str] = field(default_factory=dict)  # rel -> layer
    layer_source: str = "default"
    imports: Dict[str, List[Tuple[str, int, bool]]] = field(default_factory=dict)  # rel -> [(dotted, line, module_level)]
    parse_failures: List[str] = field(default_factory=list)
    suffix_index: Dict[str, Set[str]] = field(default_factory=dict)  # dotted suffix -> rels
    # v1.5 interprocedural
    functions: Dict[str, Any] = field(default_factory=dict)  # key -> summaries.FunctionRecord
    classes: Dict[str, Any] = field(default_factory=dict)  # key -> summaries.ClassRecord
    function_aliases: Dict[str, str] = field(default_factory=dict)  # "app.x.f" -> "src.app.x.f" (source-root variants)
    class_aliases: Dict[str, str] = field(default_factory=dict)
    relationships: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)  # model class dotted -> {attr: lazy}
    callgraph: Any = None  # callgraph.CallGraph when enabled
    interprocedural: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})

    # ---- lookups ------------------------------------------------------------
    def alias_kind(self, dotted: str) -> Optional[str]:
        return self.aliases.get(dotted)

    def factory_kind(self, dotted: str) -> Optional[str]:
        if dotted in self.factories:
            return self.factories[dotted]
        last = dotted.rsplit(".", 1)[-1]
        kinds = self.factory_last.get(last)
        if kinds and len(kinds) == 1:
            return next(iter(kinds))
        return None

    def layer_of(self, rel: str) -> str:
        return self.layers.get(rel, "other")

    def module_of_file(self, rel: str) -> Optional[str]:
        """Business module name for a file under modules_root, else None."""
        if not self.modules_root:
            return None
        prefix = self.modules_root.rstrip("/") + "/"
        if not rel.startswith(prefix):
            return None
        rest = rel[len(prefix) :]
        return rest.split("/", 1)[0] if "/" in rest else None

    def resolve_import(self, dotted: str) -> Optional[str]:
        """Map an absolute dotted import to a project file (exact, then unique suffix)."""
        candidates = [dotted]
        parts = dotted.split(".")
        if len(parts) > 1:
            candidates.append(".".join(parts[:-1]))
        if len(parts) > 2:
            candidates.append(".".join(parts[:-2]))
        for cand in candidates:
            if cand in self.module_map:
                return self.module_map[cand]
        for cand in candidates:
            hits = self.suffix_index.get(cand)
            if hits and len(hits) == 1:
                return next(iter(hits))
        return None

    def summary(self) -> Dict[str, Any]:
        layer_counts: Dict[str, int] = {}
        for layer in self.layers.values():
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
        return {
            "uses_fastapi": self.uses_fastapi,
            "uses_sqlalchemy": self.uses_sqlalchemy,
            "uses_async_sqlalchemy": self.uses_async_sqlalchemy,
            "uses_alembic": self.uses_alembic,
            "session_factories": len(self.factories),
            "session_aliases": len(self.aliases),
            "protected_routers": len(self.protected_routers),
            "global_auth_signal": self.global_auth_signal,
            "global_auth_evidence": sorted(self.global_auth_evidence),
            "import_linter": self.import_linter,
            "modules_root": {"path": self.modules_root, "source": self.modules_root_source},
            "layers": {"source": self.layer_source, "counts": dict(sorted(layer_counts.items()))},
            "config_source": self.config_source,
            "parse_failures": len(self.parse_failures),
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def load_config(root: str, explicit_path: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read ``.audit-fastapi.json`` (or an explicit file). Missing file -> {}."""
    path = explicit_path or os.path.join(root, CONFIG_FILENAME)
    if not os.path.isfile(path):
        if explicit_path:
            raise FileNotFoundError(path)
        return {}, None
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    return data, os.path.relpath(path, root).replace(os.sep, "/") if not os.path.isabs(explicit_path or "") else path


def module_name_for(rel: str, source_roots: Iterable[str]) -> List[str]:
    """Dotted names a file can be imported as (full path and with source roots stripped)."""
    posix = rel.replace(os.sep, "/")
    if posix.endswith(".py"):
        posix = posix[: -len(".py")]
    if posix.endswith("/__init__"):
        posix = posix[: -len("/__init__")]
    elif posix == "__init__":
        posix = ""
    dotted = posix.replace("/", ".")
    names = [dotted] if dotted else []
    for root in source_roots:
        prefix = root.strip("/").replace("/", ".") + "."
        if dotted.startswith(prefix):
            names.append(dotted[len(prefix) :])
    return names


def _layer_patterns(config: Dict[str, Any]) -> Tuple[List[Tuple[str, List[str]]], str]:
    layers = config.get("layers")
    if isinstance(layers, dict) and layers:
        ordered = [(str(layer), [str(g) for g in globs]) for layer, globs in layers.items() if isinstance(globs, list)]
        return ordered, "config"
    return DEFAULT_LAYER_PATTERNS, "default"


def infer_layer(rel: str, patterns: List[Tuple[str, List[str]]]) -> str:
    for layer, globs in patterns:
        if match_globs(rel, globs):
            return layer
    return "other"


def infer_modules_root(files: List[str]) -> Optional[str]:
    """Shallowest ``modules``/``features`` directory holding at least two packages."""
    children: Dict[str, Set[str]] = {}
    for rel in files:
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in MODULES_DIR_NAMES and i + 1 < len(parts) - 1:
                root = "/".join(parts[: i + 1])
                children.setdefault(root, set()).add(parts[i + 1])
    candidates = [root for root, subs in children.items() if len(subs) >= 2]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r.count("/"), r))
    return candidates[0]


def detect_import_linter(root: str) -> Optional[Dict[str, Any]]:
    """Find import-linter configuration and list contract names (read-only)."""
    for name in (".importlinter", "setup.cfg", "pyproject.toml", "tox.ini"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        section_line = None
        contracts: List[str] = []
        in_contract = False
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("[importlinter") or stripped.startswith("[tool.importlinter") or stripped.startswith("[[tool.importlinter"):
                if section_line is None:
                    section_line = i
                in_contract = stripped.startswith("[importlinter:contract") or stripped.startswith("[[tool.importlinter.contracts")
                continue
            if stripped.startswith("[") and not stripped.startswith("[tool.importlinter"):
                in_contract = False
                continue
            if in_contract:
                m = re.match(r"name\s*=\s*['\"]?([^'\"]+)['\"]?\s*$", stripped)
                if m:
                    contracts.append(m.group(1).strip())
        if section_line is not None:
            return {"file": name, "line": section_line, "contracts": contracts}
    return None


def _relative_target(module: str, is_package: bool, level: int, name: Optional[str]) -> Optional[str]:
    base_parts = module.split(".") if module else []
    if not is_package:
        base_parts = base_parts[:-1]
    up = level - 1
    if up > len(base_parts):
        return None
    base_parts = base_parts[: len(base_parts) - up] if up else base_parts
    target = ".".join(base_parts)
    if name:
        target = f"{target}.{name}" if target else name
    return target or None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #


def collect_summaries(
    facts: ProjectFacts,
    files: List[Tuple[str, str]],
    *,
    max_file_bytes: int,
    max_call_depth: int,
    time_budget_s: float,
) -> None:
    """Second read-only pass: per-function summaries and the conservative call graph."""
    import time as _time

    from .callgraph import CallGraph
    from .kinds import module_level_factories
    from .resolver import Scope as _Scope
    from .summaries import build_summaries

    start = _time.monotonic()
    truncated_reason: Optional[str] = None
    for rel, full in files:
        if _time.monotonic() - start > time_budget_s:
            truncated_reason = f"time budget of {time_budget_s:.1f}s exceeded while summarising functions"
            break
        if rel in facts.parse_failures:
            continue
        try:
            if os.stat(full).st_size > max_file_bytes:
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            tree = ast.parse(data, filename=rel)
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        imports, bound = collect_scope(tree.body)
        module_scope = _Scope("<module>", False, imports, bound)
        local_factories = module_level_factories(tree, lambda parts: resolve_in_scopes([module_scope], parts))
        module = facts.file_module.get(rel, "")
        records, classes = build_summaries(rel, module, tree, facts, local_factories)
        alt_modules = [m for m in module_name_for(rel, facts.config.get("source_roots") or ["src"]) if m != module]
        for record in records:
            facts.functions[record.key] = record
            for alt in alt_modules:
                facts.function_aliases.setdefault(f"{alt}.{record.qualname}" if alt else record.qualname, record.key)
        for cls in classes:
            facts.classes[cls.key] = cls
            short = cls.key.rsplit(".", 1)[-1]
            for alt in alt_modules:
                facts.class_aliases.setdefault(f"{alt}.{short}" if alt else short, cls.key)
    remaining = max(0.5, time_budget_s - (_time.monotonic() - start))
    graph = CallGraph(facts, max_depth=max_call_depth, time_budget_s=remaining).build()
    if truncated_reason and not graph.stats.truncated:
        graph.stats.truncated, graph.stats.truncated_reason = True, truncated_reason
    graph.stats.elapsed_ms = int((_time.monotonic() - start) * 1000)
    facts.callgraph = graph
    facts.interprocedural = graph.stats.to_dict()


def collect_facts(
    root: str,
    files: List[Tuple[str, str]],
    *,
    config: Optional[Dict[str, Any]] = None,
    config_source: Optional[str] = None,
    max_file_bytes: int = 5 * 1024 * 1024,
    interprocedural: bool = True,
    max_call_depth: int = 4,
    time_budget_s: float = 5.0,
) -> ProjectFacts:
    facts = ProjectFacts(root=os.path.abspath(root), config=dict(config or {}), config_source=config_source)
    facts.files = [rel for rel, _ in files]
    source_roots = facts.config.get("source_roots") or ["src"]
    patterns, facts.layer_source = _layer_patterns(facts.config)

    configured_root = facts.config.get("modules_root")
    if isinstance(configured_root, str) and configured_root.strip():
        facts.modules_root = configured_root.strip().strip("/")
        facts.modules_root_source = "config"
    else:
        facts.modules_root = infer_modules_root(facts.files)
        facts.modules_root_source = "inferred" if facts.modules_root else "none"

    facts.import_linter = detect_import_linter(facts.root)

    for rel, _ in files:
        facts.layers[rel] = infer_layer(rel, patterns)
        names = module_name_for(rel, source_roots)
        if names:
            facts.file_module[rel] = names[0]
            for name in names:
                facts.module_map.setdefault(name, rel)
                parts = name.split(".")
                for i in range(1, len(parts)):  # proper suffixes: "b.c", "c" for "a.b.c"
                    facts.suffix_index.setdefault(".".join(parts[i:]), set()).add(rel)

    for rel, full in files:
        try:
            if os.stat(full).st_size > max_file_bytes:
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            tree = ast.parse(data, filename=rel)
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            facts.parse_failures.append(rel)
            continue
        _collect_from_tree(facts, rel, tree)

    facts.parse_failures.sort()
    if interprocedural:
        collect_summaries(facts, files, max_file_bytes=max_file_bytes, max_call_depth=max_call_depth, time_budget_s=time_budget_s)
    else:
        facts.interprocedural = {"enabled": False, "max_depth": max_call_depth}
    return facts


def _collect_from_tree(facts: ProjectFacts, rel: str, tree: ast.Module) -> None:
    module = facts.file_module.get(rel, "")
    is_package = rel.endswith("__init__.py")
    imports, bound = collect_scope(tree.body)
    scopes = [Scope("<module>", False, imports, bound)]

    def resolve(parts):
        return resolve_in_scopes(scopes, parts)

    if module_imports(tree, ("fastapi",)):
        facts.uses_fastapi = True
    if module_imports(tree, ("sqlalchemy", "sqlmodel")):
        facts.uses_sqlalchemy = True
    if module_imports(tree, ("sqlalchemy.ext.asyncio",)):
        facts.uses_async_sqlalchemy = True
    if module_imports(tree, ("alembic",)):
        facts.uses_alembic = True

    # import graph: module-level versus function-level. Imports guarded by
    # `if TYPE_CHECKING:` never run, so they are not module-level edges (no cycle).
    edges: List[Tuple[str, int, bool]] = []
    type_checking_ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else None
            if name == "TYPE_CHECKING":
                type_checking_ids |= {id(n) for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))}
    module_level_ids = {
        id(n) for n in iter_scope_nodes(tree.body)
        if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in type_checking_ids
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append((alias.name, node.lineno, id(node) in module_level_ids))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                edges.append((node.module, node.lineno, id(node) in module_level_ids))
                for alias in node.names:
                    if alias.name != "*":
                        edges.append((f"{node.module}.{alias.name}", node.lineno, id(node) in module_level_ids))
            elif node.level > 0:
                base = _relative_target(module, is_package, node.level, node.module)
                if base:
                    edges.append((base, node.lineno, id(node) in module_level_ids))
                    for alias in node.names:
                        if alias.name != "*":
                            edges.append((f"{base}.{alias.name}", node.lineno, id(node) in module_level_ids))
    facts.imports[rel] = edges

    # ORM relationships per model class (for the async lazy-load rule)
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            continue
        rels: Dict[str, Optional[str]] = {}
        for item in stmt.body:
            value = item.value if isinstance(item, (ast.Assign, ast.AnnAssign)) else None
            if not isinstance(value, ast.Call):
                continue
            parts = dotted_name(value.func)
            dotted = resolve(parts) if parts else None
            if dotted not in {"sqlalchemy.orm.relationship", "sqlalchemy.orm.relationships.relationship", "sqlmodel.Relationship"}:
                continue
            names = [t.id for t in item.targets if isinstance(t, ast.Name)] if isinstance(item, ast.Assign) else ([item.target.id] if isinstance(item.target, ast.Name) else [])
            lazy = None
            for kw in value.keywords:
                if kw.arg == "lazy" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    lazy = kw.value.value
            for name in names:
                rels[name] = lazy
        if rels:
            facts.relationships[f"{module}.{stmt.name}" if module else stmt.name] = rels

    # module-level bindings: factories, Annotated aliases, protected routers
    for stmt in tree.body:
        targets: List[ast.Name] = []
        value: Optional[ast.AST] = None
        annotation: Optional[ast.AST] = None
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets = [stmt.target]
            value = stmt.value
            annotation = stmt.annotation
        if not targets:
            continue
        # X = Annotated[AsyncSession, Depends(...)]  or  X: TypeAlias = Annotated[...]
        alias_kind = None
        if isinstance(value, ast.Subscript):
            dotted = annotation_to_dotted(value, resolve)
            if dotted in TYPE_KINDS:
                alias_kind = TYPE_KINDS[dotted]
            # any Annotated[..., Depends(f)] alias: remember f so auth-looking dependencies are
            # recognised through the alias from other modules (MemberDep, CurrentUser, ...)
            for sub in ast.walk(value):
                if isinstance(sub, ast.Call):
                    cparts = dotted_name(sub.func)
                    if cparts and cparts[-1] in {"Depends", "Security"} and sub.args:
                        inner = dotted_name(sub.args[0])
                        if inner:
                            for t in targets:
                                facts.dependency_aliases[f"{module}.{t.id}" if module else t.id] = resolve(inner) or inner[-1]
        if alias_kind:
            for t in targets:
                facts.aliases[f"{module}.{t.id}" if module else t.id] = alias_kind
            continue
        if isinstance(value, ast.Call):
            parts = dotted_name(value.func)
            dotted = resolve(parts) if parts else None
            if dotted in FACTORY_KINDS:
                for t in targets:
                    key = f"{module}.{t.id}" if module else t.id
                    facts.factories[key] = FACTORY_KINDS[dotted]
                    facts.factory_last.setdefault(t.id, set()).add(FACTORY_KINDS[dotted])
            elif dotted in {"fastapi.APIRouter", "fastapi.routing.APIRouter"} and _has_keyword(value, "dependencies"):
                for t in targets:
                    facts.protected_routers.add(f"{module}.{t.id}" if module else t.id)
            elif dotted in {"fastapi.FastAPI", "fastapi.applications.FastAPI"} and _has_keyword(value, "dependencies"):
                facts.global_auth_signal = True
                facts.global_auth_evidence.append(f"{rel}:{stmt.lineno} FastAPI(dependencies=...)")
        _ = annotation

    # global auth signals anywhere in the module
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "include_router" and _has_keyword(node, "dependencies"):
            facts.global_auth_signal = True
            facts.global_auth_evidence.append(f"{rel}:{node.lineno} include_router(dependencies=...)")
        elif attr == "add_middleware" and node.args:
            mw = dotted_name(node.args[0])
            label = ".".join(mw) if mw else ""
            if label and AUTH_RE.search(label.split(".")[-1]):
                facts.global_auth_signal = True
                facts.global_auth_evidence.append(f"{rel}:{node.lineno} add_middleware({label})")
        elif attr == "middleware" and node.args:
            pass  # @app.middleware("http") functions are checked by name below
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "middleware":
                    if AUTH_RE.search(node.name):
                        facts.global_auth_signal = True
                        facts.global_auth_evidence.append(f"{rel}:{node.lineno} @middleware {node.name}")
